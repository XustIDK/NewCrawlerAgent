"""Agent 运行时：确定性抓取流水线 + DeepSeek Function Calling Agent Loop。

- AgentSession：一次任务的全部共享状态（配置、站点画像、发现结果、CrawlResult、工具轨迹），
  tools.execute_tool 只依赖这个对象；
- run_deterministic：不需要 LLM 的完整流水线（发现 → 抽取 → 输出），异常时仍尽量落盘；
- run_agent：DeepSeek Function Calling 循环（模型决策 → 本地工具 → 结果回传），
  无 API Key、客户端异常或循环失败时降级为确定性流水线，保证离线可用。

接口契约见 DESIGN.md 第 4.6、9.6 节。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from typing import Any, Callable

from . import config as config_module
from .errors import NewsAgentError
from .models import CrawlConfig, CrawlResult, NewsItem, SiteProfile
from .tools import TOOL_SCHEMAS, execute_tool
from .urls import domain_from_url, normalize_entry_url

# 字段完整率统计顺序（与报告/流程图保持一致）。
REQUIRED_FIELDS = ("title", "body", "published_at", "author", "source", "language")
FIELD_LABELS = {
    "title": "标题",
    "body": "正文",
    "published_at": "发布时间",
    "author": "作者",
    "source": "来源",
    "language": "语言",
}

MAX_CRAWL_TOOL_LIMIT = 50


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _emit(callback: Callable[..., Any] | None, message: str, phase: str | None = None) -> None:
    """调用进度回调；兼容只接受一个参数的回调，且任何异常都不影响主流程。"""
    if not callback:
        return
    try:
        callback(message, phase)
    except TypeError:
        try:
            callback(message)
        except Exception:
            pass
    except Exception:
        pass


def _truncate(text: Any, limit: int = 80) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: max(1, limit - 1)] + "…"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _parse_tool_arguments(raw: Any) -> Any:
    """把模型的工具参数转成可写入 trace 的 JSON 值（解析失败时保留原文）。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {"_raw": _truncate(raw, 400)}
    return {}


def _discovery_label(method: str) -> str:
    labels = {
        "profile": "站点配置",
        "feed": "RSS/Atom",
        "jsonld": "JSON-LD",
        "heuristic": "链接聚类",
        "mixed": "混合策略",
        "sitemap": "站点地图",
        "llm": "模型推断",
    }
    return labels.get(str(method or ""), str(method or "未记录"))


class _BrowserFetchProxy:
    """把 discover.probe_structure 内部的抓取强制切换成浏览器模式。

    probe_structure 的签名只接收 fetcher，这里用一个小代理实现 inspect_site(use_browser=true)，
    不修改 Fetcher 的配置，也不影响其他调用。
    """

    def __init__(self, fetcher: Any) -> None:
        self._fetcher = fetcher

    def get(self, url: str, **kwargs: Any) -> Any:
        kwargs["browser"] = True
        return self._fetcher.get(url, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fetcher, name)


# ---------------------------------------------------------------------------
# AgentSession
# ---------------------------------------------------------------------------

class AgentSession:
    """一次抓取任务的共享状态与工具实现。"""

    def __init__(
        self,
        config: CrawlConfig,
        settings: dict[str, Any] | None = None,
        on_event: Callable[..., Any] | None = None,
        intervention_event: Any = None,
        fetcher: Any = None,
        profile: SiteProfile | None = None,
    ) -> None:
        if not isinstance(config, CrawlConfig):
            raise NewsAgentError("AgentSession 需要 CrawlConfig 对象")
        self.config = config
        self.settings = settings if settings is not None else config_module.load_settings()
        self.on_event = on_event
        self.intervention_event = intervention_event
        self.fetcher = fetcher
        self._owns_fetcher = False
        self.profile: SiteProfile | None = profile
        self._profile_loaded = profile is not None
        self.result = CrawlResult(entry_url=config.entry_url, domain=config.domain)
        self.discovery: Any = None
        self.candidates: list[NewsItem] = []
        self.next_index = 0
        self.seen_urls: set[str] = set()
        self.trace: list[dict[str, Any]] = []
        self.output_files: dict[str, str] = {}
        self.report_markdown_used = ""
        self.saved_article_count = 0
        self.last_discovery_url = ""
        self._closed = False

    # -- 基础属性 ----------------------------------------------------------

    @property
    def stats(self) -> Any:
        return self.result.stats

    @property
    def remaining_candidates(self) -> int:
        return max(0, len(self.candidates) - self.next_index)

    @property
    def has_outputs(self) -> bool:
        return bool(self.output_files.get("dir"))

    def emit(self, message: str, phase: str | None = None) -> None:
        _emit(self.on_event, message, phase)

    def _bridge_event(self, message: str, phase: str = "fetching") -> None:
        """把 Fetcher 的两参数进度事件转发给上层（兼容只接受单参数的回调）。"""
        _emit(self.on_event, message, phase)

    # -- 抓取资源 ----------------------------------------------------------

    def ensure_fetcher(self) -> Any:
        """懒创建抓取器（Fetcher 由 crawler-net 按 DESIGN 9.1 实现）。"""
        if self.fetcher is not None:
            return self.fetcher
        from .fetch import Fetcher

        self.fetcher = Fetcher(
            self.config,
            settings=self.settings,
            on_event=self._bridge_event,
            intervention_event=self.intervention_event,
        )
        self._owns_fetcher = True
        return self.fetcher

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.fetcher is not None and self._owns_fetcher:
            try:
                self.fetcher.close()
            except Exception:
                pass

    def _diagram_renderer(self) -> Callable[..., Any] | None:
        if self.fetcher is None:
            return None
        try:
            renderer = getattr(self.fetcher, "render_svg_screenshot", None)
        except Exception:
            return None
        return renderer if callable(renderer) else None

    # -- 站点画像 ----------------------------------------------------------

    def load_profile(self) -> SiteProfile | None:
        """按域名从 sites/*.json 加载站点画像；失败只记警告，不影响通用策略。"""
        if self._profile_loaded:
            return self.profile
        self._profile_loaded = True
        try:
            from .profiles import load_profile

            self.profile = load_profile(self.config.domain)
        except Exception as error:
            self.stats.add_warning(f"读取站点配置失败：{type(error).__name__}: {error}")
            self.profile = None
        if self.profile is not None:
            self.result.profile_used = self.profile.domain
        return self.profile

    def _canonical(self, url: str) -> str:
        try:
            from .normalize import canonicalize_url

            return canonicalize_url(url) or url
        except Exception:
            return (url or "").split("#", 1)[0]

    def _article_fetch_options(self) -> tuple[bool | None, str, str]:
        """与 extract.extract_article 完全一致的抓取参数。

        参数一致才能命中 Fetcher 的运行内缓存，避免“预抓取校验 + 正式抽取”重复请求。
        """
        profile = self.profile
        mode = str(profile.fetch.get("mode") or "").lower() if profile is not None else ""
        if mode not in ("auto", "always", "never"):
            mode = ""
        if not mode:
            mode = str(getattr(self.config, "browser_mode", "auto") or "auto").lower()
        if mode == "always":
            browser: bool | None = True
        elif mode == "never":
            browser = False
        else:
            browser = None
        wait_selector = ""
        encoding = ""
        if profile is not None:
            wait_selector = str(profile.fetch.get("wait_selector") or "")
            raw_encoding = str(profile.fetch.get("encoding") or "").lower()
            if raw_encoding and raw_encoding != "auto":
                encoding = raw_encoding
        return browser, wait_selector, encoding

    def _precheck_candidate(self, item_url: str) -> tuple[str, str]:
        """抓取候选文章页并校验最终 URL（Fetcher 有运行内缓存，后续抽取不会重复请求）。

        返回 (跳过原因, 错误信息)，两者都为空表示可以继续抽取：
        - 重定向到根路径/频道首页等非文章页：返回跳过原因，避免把站点首页当文章；
        - 抓取失败：返回错误信息，由 crawl() 按原逻辑记录错误并继续下一个候选。
        """
        getter = getattr(self.fetcher, "get", None) if self.fetcher is not None else None
        if not callable(getter):
            return "", ""
        browser, wait_selector, encoding = self._article_fetch_options()
        try:
            fetched = getter(
                item_url, browser=browser, wait_selector=wait_selector, encoding=encoding
            )
        except Exception as error:
            return "", f"{type(error).__name__}: {error}"
        if fetched is None:
            # 兼容测试替身：没有返回 FetchResult 时交给 extract_article 走原逻辑。
            return "", ""
        if not getattr(fetched, "ok", False):
            error_text = str(getattr(fetched, "error", "") or "未知错误")
            return "", f"FetchError: 抓取失败：{item_url}（{error_text}）"
        final_url = str(getattr(fetched, "final_url", "") or "")
        if not final_url:
            return "", ""
        try:
            if self._canonical(final_url) == self._canonical(item_url):
                return "", ""
            from . import discover

            checker = getattr(discover, "looks_like_article_url", None)
            if not callable(checker) or checker(final_url, self.config):
                return "", ""
        except Exception:
            return "", ""
        return f"候选重定向到非文章页，已跳过：{item_url} -> {final_url}", ""

    # -- 发现 --------------------------------------------------------------

    def ensure_discovery(self, url: str | None = None) -> Any:
        """发现新闻列表；profile 自动从 sites/ 加载，结果写入 session 与 stats。"""
        target = normalize_entry_url(url or self.config.entry_url)
        self.ensure_fetcher()
        self.load_profile()
        from . import discover

        self.emit(f"正在发现新闻列表：{_truncate(target, 100)}", "discovering")
        discovery = discover.discover_news(
            self.fetcher,
            target,
            self.config,
            profile=self.profile,
            settings=self.settings,
            on_event=self.on_event,
        )
        if discovery is None:
            raise NewsAgentError("新闻列表发现没有返回结果")
        items = list(getattr(discovery, "items", []) or [])
        pages = list(getattr(discovery, "pages", []) or [])
        method = str(getattr(discovery, "method", "") or "")

        # 候选去重：URL 归一 + 跳过已成功抽取的文章。
        known = {self._canonical(article.canonical_url or article.url) for article in self.result.articles}
        deduped: list[NewsItem] = []
        seen: set[str] = set()
        for item in items:
            item_url = str(getattr(item, "url", "") or "")
            key = self._canonical(item_url)
            if not key or key in seen or key in known:
                continue
            seen.add(key)
            deduped.append(item)

        self.discovery = discovery
        self.candidates = deduped
        self.next_index = 0
        self.last_discovery_url = target
        self.result.discovery_method = method
        self.result.feeds = [str(feed) for feed in (getattr(discovery, "feeds", []) or [])]
        if self.profile is not None:
            self.result.profile_used = self.profile.domain

        self.stats.pages_fetched += max(1, len(pages))
        self.stats.list_pages += max(1, len(pages))
        self.stats.candidates = len(deduped)
        self.stats.note_method("discovery", method or "unknown")
        for warning in getattr(discovery, "warnings", []) or []:
            self.stats.add_warning(f"发现阶段：{warning}")
        if domain_from_url(target) != self.config.domain:
            self.stats.add_warning(
                f"发现页面域名（{domain_from_url(target)}）与任务域名（{self.config.domain}）不同"
            )
        self.emit(
            f"发现 {len(deduped)} 个候选链接（方式：{_discovery_label(method)}）",
            "discovering",
        )
        return discovery

    # -- 抽取 --------------------------------------------------------------

    def crawl(self, limit: int | None = None, url: str | None = None) -> dict[str, Any]:
        """对候选列表逐条抽取文章；成功写入 result.articles，失败记录错误后继续。

        抽取前会先抓取候选页并校验最终 URL：若重定向到根路径/频道首页等非文章页，
        只记 warning 并跳过该候选（避免 RSS 跳转链接把站点首页当文章）。
        """
        self.ensure_fetcher()
        if url:
            target = normalize_entry_url(url)
            if target != self.last_discovery_url:
                self.ensure_discovery(target)
        elif self.discovery is None:
            self.ensure_discovery()

        total_articles = len(self.result.articles)
        target_total = self.config.max_articles
        quota = max(0, target_total - total_articles)
        if limit is None:
            want = quota
        else:
            want = max(0, min(_as_int(limit, quota), quota, MAX_CRAWL_TOOL_LIMIT))

        summary: dict[str, Any] = {
            "added": 0,
            "attempted": 0,
            "total_articles": total_articles,
            "target": target_total,
            "remaining_candidates": self.remaining_candidates,
            "samples": [],
            "warnings": [],
            "missing_hint": "",
        }
        if want <= 0:
            summary["warnings"].append(f"已达到目标篇数上限（{target_total} 篇）")
            self.emit("已达到目标篇数上限，无需继续抓取。", "extracting")
            return summary
        if not self.candidates:
            summary["warnings"].append("没有可抓取的候选链接")
            self.emit("没有可抓取的候选链接，请尝试 inspect_site 查看页面结构。", "extracting")
            return summary

        from .extract import extract_article

        added = []
        warnings: list[str] = []
        while (
            self.next_index < len(self.candidates)
            and len(added) < want
            and len(self.result.articles) < target_total
        ):
            item = self.candidates[self.next_index]
            self.next_index += 1
            item_url = str(getattr(item, "url", "") or "")
            try:
                canonical = self._canonical(item_url)
            except Exception:
                canonical = item_url
            if canonical and canonical in self.seen_urls:
                self.stats.duplicates += 1
                continue
            summary["attempted"] += 1
            title_hint = getattr(item, "title", "") or item_url
            self.emit(
                f"正在抽取第 {len(self.result.articles) + 1}/{target_total} 篇：{_truncate(title_hint, 70)}",
                "extracting",
            )
            # 抓取后最终 URL 校验：RSS 跳转链接常常 302 到站点首页，
            # 这种候选必须跳过，否则首页会被当成新闻文章写进数据集。
            skip_reason, fetch_error = self._precheck_candidate(item_url)
            if skip_reason:
                self.stats.add_warning(skip_reason)
                warnings.append(skip_reason)
                self.emit(skip_reason, "extracting")
                continue
            if fetch_error:
                self.stats.failed += 1
                self.stats.add_error(item_url, "extract", fetch_error)
                warnings.append(
                    f"抽取失败：{_truncate(item_url, 80)} —— {_truncate(fetch_error, 120)}"
                )
                self.emit(
                    f"抽取失败：{_truncate(item_url, 80)}（{_truncate(fetch_error, 100)}）",
                    "extracting",
                )
                continue
            try:
                article = extract_article(
                    self.fetcher,
                    item,
                    self.config,
                    profile=self.profile,
                    settings=self.settings,
                    on_event=self.on_event,
                )
            except Exception as error:  # 单篇失败继续下一篇
                message = f"{type(error).__name__}: {error}"
                self.stats.failed += 1
                self.stats.add_error(item_url, "extract", message)
                warnings.append(f"抽取失败：{_truncate(item_url, 80)} —— {_truncate(message, 120)}")
                self.emit(f"抽取失败：{_truncate(item_url, 80)}（{_truncate(message, 100)}）", "extracting")
                continue
            if article is None:
                self.stats.failed += 1
                self.stats.add_error(item_url, "extract", "抽取结果为空")
                warnings.append(f"抽取结果为空：{_truncate(item_url, 80)}")
                continue
            title = str(getattr(article, "title", "") or "")
            body = str(getattr(article, "body", "") or "")
            if not title and len(body) < 80:
                self.stats.failed += 1
                self.stats.add_error(item_url, "extract", "标题与正文均缺失")
                warnings.append(f"标题与正文均缺失：{_truncate(item_url, 80)}")
                continue
            article_key = self._canonical(
                str(getattr(article, "canonical_url", "") or getattr(article, "url", "") or item_url)
            )
            if article_key and article_key in self.seen_urls:
                self.stats.duplicates += 1
                continue
            if not getattr(article, "url", ""):
                article.url = item_url
            self.seen_urls.add(article_key or item_url)
            self.result.add_article(article)
            added.append(article)
            self.stats.extracted = len(self.result.articles)
            self.stats.note_method("extract", str(getattr(article, "extract_method", "") or "unknown"))
            self.emit(
                f"已抽取：{_truncate(getattr(article, 'title', '') or item_url, 70)}"
                f"（累计 {len(self.result.articles)} 篇）",
                "extracting",
            )

        self.stats.extracted = len(self.result.articles)
        primary = added[:5]
        summary.update({
            "added": len(added),
            "attempted": summary["attempted"],
            "total_articles": len(self.result.articles),
            "remaining_candidates": self.remaining_candidates,
            "samples": [
                {
                    "title": _truncate(getattr(article, "title", "") or "（无标题）", 120),
                    "published_at": str(getattr(article, "published_at", "") or "")[:19],
                    "source": _truncate(getattr(article, "source", "") or "", 60),
                    "url": str(getattr(article, "url", "") or ""),
                }
                for article in primary
            ],
            "warnings": warnings[:8],
        })
        summary["missing_hint"] = self._missing_hint(added)
        self.emit(
            f"本轮新增 {len(added)} 篇，累计 {len(self.result.articles)}/{target_total} 篇",
            "extracting",
        )
        return summary

    def _missing_hint(self, articles: list[Any]) -> str:
        if not articles:
            return ""
        missing = []
        for field in REQUIRED_FIELDS:
            count = sum(1 for article in articles if not getattr(article, field, ""))
            if count:
                missing.append(f"{FIELD_LABELS[field]} {count} 篇")
        if not missing:
            return "本次新增文章关键字段完整"
        return "本次新增文章中缺少：" + "、".join(missing)

    # -- 结构探测 / 自检 / 画像 / 输出 -------------------------------------

    def inspect(self, url: str | None = None, use_browser: bool = False) -> dict[str, Any]:
        """探测站点结构（RSS / JSON-LD / 列表选择器 / 文章页字段），供模型写站点配置。"""
        self.ensure_fetcher()
        self.load_profile()
        from . import discover

        target = normalize_entry_url(url or self.config.entry_url)
        fetcher = _BrowserFetchProxy(self.fetcher) if use_browser else self.fetcher
        self.emit(f"正在探测站点结构：{_truncate(target, 100)}", "analyzing")
        info = discover.probe_structure(
            fetcher,
            target,
            self.config,
            profile=self.profile,
            settings=self.settings,
        )
        self.stats.pages_fetched += 1
        if not isinstance(info, dict):
            return {"error": "结构探测没有返回有效结果"}
        result = dict(info)
        result.setdefault("url", target)
        return result

    def analyze(self) -> dict[str, Any]:
        """字段完整率、语言分布、时间跨度、失败摘要、剩余候选与下一步建议。"""
        articles = list(self.result.articles)
        total = len(articles)
        completeness: dict[str, dict[str, Any]] = {}
        for field in REQUIRED_FIELDS:
            count = sum(1 for article in articles if getattr(article, field, ""))
            completeness[field] = {
                "count": count,
                "rate": round(count / total, 4) if total else 0.0,
            }
        languages = [
            {"language": language, "count": count}
            for language, count in Counter(
                str(getattr(article, "language", "") or "未知") for article in articles
            ).most_common()
        ]
        dates = sorted({
            str(getattr(article, "published_at", "") or "")[:10]
            for article in articles
            if re.match(r"^\d{4}-\d{2}-\d{2}", str(getattr(article, "published_at", "") or ""))
        })
        date_span: dict[str, Any] = {}
        if dates:
            try:
                earliest = datetime.strptime(dates[0], "%Y-%m-%d")
                latest = datetime.strptime(dates[-1], "%Y-%m-%d")
                date_span = {
                    "earliest": dates[0],
                    "latest": dates[-1],
                    "days": (latest - earliest).days + 1,
                }
            except ValueError:
                date_span = {"earliest": dates[0], "latest": dates[-1], "days": 0}

        errors = list(self.stats.errors)
        error_summary = {
            "total": len(errors),
            "by_stage": dict(Counter(str(error.get("stage", "")) for error in errors)),
            "recent": [
                {
                    "stage": str(error.get("stage", ""))[:30],
                    "url": _truncate(error.get("url", ""), 100),
                    "error": _truncate(error.get("error", ""), 160),
                }
                for error in errors[:5]
            ],
        }

        suggestions: list[str] = []
        if total == 0:
            if self.candidates:
                suggestions.append(
                    "尚未抽取到文章：再次调用 crawl_site 补抓；若仍失败，用 inspect_site 查看结构后 save_site_profile 修正配置。"
                )
            else:
                suggestions.append(
                    "没有发现候选文章：用 inspect_site 检查 URL 是否为新闻列表页，必要时 use_browser=true 或改为站点 RSS 地址。"
                )
        if total and completeness["body"]["rate"] < 0.8:
            suggestions.append(
                "正文完整率偏低：inspect_site 查看 article_page_probe，再用 save_site_profile 指定正文选择器后补抓。"
            )
        if total and completeness["published_at"]["rate"] < 0.6:
            suggestions.append("发布时间缺失较多：用 save_site_profile 配置 article.date 选择器（可带 ::attr(datetime)）。")
        if total and completeness["title"]["rate"] < 0.9:
            suggestions.append("标题缺失：检查 article.title 选择器，或确认列表页链接是否指向文章详情页。")
        if total and completeness["language"]["rate"] < 0.8:
            suggestions.append("语言字段缺失：可在站点配置中填写 language，或在报告中说明自动识别限制。")
        if total and total >= self.config.max_articles:
            suggestions.append("已达到目标篇数：调用 save_outputs 保存数据集与报告。")
        elif self.remaining_candidates > 0:
            suggestions.append(
                f"仍有 {self.remaining_candidates} 个候选未抓取，可再次调用 crawl_site 补抓。"
            )
        if errors:
            suggestions.append(f"存在 {len(errors)} 条抽取错误，撰写报告时需如实说明失败原因。")

        rate_text = " · ".join(
            f"{FIELD_LABELS[field]} {completeness[field]['rate'] * 100:.0f}%"
            for field in ("title", "body", "published_at")
        )
        self.emit(f"覆盖自检：{total}/{self.config.max_articles} 篇（{rate_text}）", "analyzing")
        return {
            "article_count": total,
            "target": self.config.max_articles,
            "completeness": completeness,
            "languages": languages,
            "date_span": date_span,
            "discovery_method": self.result.discovery_method,
            "extract_methods": dict(self.stats.extract_methods),
            "remaining_candidates": self.remaining_candidates,
            "error_summary": error_summary,
            "suggestions": suggestions,
        }

    def apply_profile(self, data: dict[str, Any]) -> dict[str, Any]:
        """校验并保存站点配置，然后替换当前画像（下次 crawl 生效）。"""
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError as error:
                raise NewsAgentError(f"站点配置不是合法 JSON：{error}") from error
        if not isinstance(data, dict):
            raise NewsAgentError("站点配置必须是 JSON 对象")
        profile_data = dict(data)
        profile_data.setdefault("domain", self.config.domain)
        from . import profiles

        profile = profiles.validate_profile(profile_data)
        current = self.config.domain
        saved_domain = profile.domain
        related = (
            saved_domain == current
            or current.endswith("." + saved_domain)
            or saved_domain.endswith("." + current)
        )
        if not related:
            raise NewsAgentError(
                f"站点配置域名（{saved_domain}）与当前任务域名（{current}）不一致；"
                "请省略 domain 或填写当前站点域名。"
            )
        if not profile.learned_by:
            profile.learned_by = "agent"
        if not profile.note:
            profile.note = "由 Agent 通过 inspect_site 推断生成，建议人工复核主要选择器。"
        path = profiles.save_profile(profile)
        self.profile = profile
        self._profile_loaded = True
        self.result.profile_used = profile.domain
        # 新配置需要重新发现，才能用上列表选择器/翻页规则。
        self.discovery = None
        self.candidates = []
        self.next_index = 0
        self.emit(f"站点配置已保存并加载：{path}", "saving")
        return {
            "saved": True,
            "domain": profile.domain,
            "path": str(path),
            "learned_by": profile.learned_by,
            "note": "配置已保存并加载，请再次调用 crawl_site 补抓。",
        }

    def save_outputs(self, report_markdown: str | None = None) -> dict[str, str]:
        """落盘数据集 + 报告 + 流程图；多次调用时复用同一输出目录。"""
        from . import output

        report = str(report_markdown).strip() if report_markdown else ""
        if report:
            self.report_markdown_used = report
        effective_report = report or self.report_markdown_used or None
        paths = output.write_outputs(
            self.result,
            self.config,
            settings=self.settings,
            report_markdown=effective_report,
            trace=self.trace,
            renderer=self._diagram_renderer(),
            output_dir=self.output_files.get("dir") or None,
        )
        self.output_files = dict(paths)
        self.result.output_files = dict(paths)
        self.saved_article_count = len(self.result.articles)
        self.emit(f"已保存输出：{paths.get('dir', '')}", "saving")
        return paths

    def refresh_diagram(self) -> None:
        """用循环结束后的完整 trace 重新生成 Agent Loop 图。

        save_outputs 作为工具在循环内执行时会先生成一张图，此时本轮调用尚未写回
        trace（会显示“未知工具”）。循环结束后重绘可保证流程图包含最终工具调用。
        """
        if not self.has_outputs:
            return
        out_dir = self.output_files.get("dir")
        if not out_dir:
            return
        from . import output

        try:
            svg_path, png_path = output.save_agent_loop_diagram(
                self.trace, self.result, out_dir, renderer=self._diagram_renderer()
            )
        except Exception as error:  # 绘图失败不影响数据产物
            self.stats.add_warning(f"流程图重绘失败：{type(error).__name__}: {error}")
            return
        if svg_path:
            self.output_files["diagram_svg"] = svg_path
            self.result.output_files["diagram_svg"] = svg_path
        if png_path:
            self.output_files["diagram_png"] = png_path
            self.result.output_files["diagram_png"] = png_path


# ---------------------------------------------------------------------------
# 报告辅助
# ---------------------------------------------------------------------------

def _template_report(result: CrawlResult, note: str = "") -> str:
    from . import output

    report = output.build_template_report(result)
    if note:
        report = report.rstrip() + f"\n\n> **运行说明**：{note}\n"
    return report


def _report_with_note(report: str, note: str) -> str:
    text = (report or "").strip()
    if not text:
        return ""
    return text.rstrip() + f"\n\n> **运行说明**：{note}\n"


# ---------------------------------------------------------------------------
# 确定性流水线
# ---------------------------------------------------------------------------

def run_deterministic(
    config: CrawlConfig,
    settings: dict[str, Any] | None = None,
    on_event: Callable[..., Any] | None = None,
    intervention_event: Any = None,
) -> CrawlResult:
    """不依赖 LLM 的确定性流水线：发现 → 抽取 → 自检 → 输出。

    任何异常都会被记录并尽量写出 run_meta.json / 模板报告，不会把异常抛给调用方。
    """
    return _run_deterministic_core(config, settings, on_event, intervention_event)


def _run_deterministic_core(
    config: CrawlConfig,
    settings: dict[str, Any] | None,
    on_event: Callable[..., Any] | None,
    intervention_event: Any,
    note: str = "",
) -> CrawlResult:
    settings = settings if settings is not None else config_module.load_settings()
    session = AgentSession(config, settings, on_event, intervention_event)
    _emit(on_event, f"开始确定性抓取：{config.entry_url}", "starting")
    failed = False
    try:
        session.ensure_discovery()
        session.crawl(limit=config.max_articles)
        coverage = session.analyze()
        if not session.result.articles:
            session.stats.add_warning("未能抽取到任何文章，请查看 errors / warnings 后重试或补充站点配置。")
        rate_text = " · ".join(
            f"{FIELD_LABELS[field]} {coverage['completeness'][field]['rate'] * 100:.0f}%"
            for field in ("title", "body", "published_at")
        )
        _emit(
            on_event,
            f"字段完整率自检：{rate_text}（共 {coverage['article_count']} 篇）",
            "analyzing",
        )
        session.stats.finish()
        report = _template_report(session.result, note)
        session.save_outputs(report)
    except Exception as error:  # noqa: BLE001 - 兜底：任何异常都记录并尽量写 run_meta
        failed = True
        message = f"{type(error).__name__}: {error}"
        session.stats.add_error(config.entry_url, "fatal", message)
        session.stats.add_warning(f"抓取流程异常：{message}")
        session.stats.finish()
        _emit(on_event, f"抓取过程出现异常：{message}", "failed")
        try:
            fallback_note = note or "抓取流程异常，本报告为兜底产物，请查看 run_meta.json 中的错误信息。"
            session.save_outputs(_template_report(session.result, fallback_note))
        except Exception as save_error:  # 输出都失败时只保留内存中的结果
            session.stats.add_warning(f"兜底输出保存失败：{type(save_error).__name__}: {save_error}")
    finally:
        session.close()

    output_dir = session.output_files.get("dir", "")
    if failed:
        _emit(
            on_event,
            f"任务失败（已尽力写入兜底报告与 run_meta.json：{output_dir or '未生成'}）",
            "failed",
        )
    elif session.output_files:
        _emit(
            on_event,
            f"抓取完成：{len(session.result.articles)} 篇文章，输出目录 {output_dir}",
            "completed",
        )
    else:
        _emit(on_event, "任务失败：未生成任何输出文件，请检查依赖模块与磁盘权限。", "failed")
    return session.result


# ---------------------------------------------------------------------------
# Agent Loop
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "你是一个通用新闻抓取 Agent，负责把用户指定的新闻网站变成结构化数据集（不限国家、语言、站点结构）。\n"
    "工作流程：\n"
    "1. 先调用 crawl_site 发现新闻列表并抽取文章正文；\n"
    "2. 调用 analyze_coverage 检查字段完整率、失败原因、语言/时间分布与剩余候选；\n"
    "3. 数据不足或字段缺失严重时，先调用 inspect_site 查看页面结构，"
    "再调用 save_site_profile 写入站点配置，然后再次 crawl_site 补抓；\n"
    "4. 最后必须调用 save_outputs 保存结构化数据集与中文报告。\n"
    "\n报告要求（Markdown）：采集概况、字段完整率、语言分布与发布时间范围、"
    "主要文章列表（标题 + 真实链接）、问题与局限。\n"
    "\n硬性约束：不得编造任何文章、标题、时间、作者或链接；报告中引用的链接必须来自工具返回的真实 URL；"
    "没有抓到文章时如实说明原因和已尝试的方法；不要超过用户设定的抓取数量上限。"
    "工具返回 error 时先判断原因再调整策略，不要重复同样的无效调用。"
)


def _user_prompt(session: AgentSession) -> str:
    profile_name = session.profile.domain if session.profile else "无（使用通用发现/抽取策略）"
    language = session.config.language_hint or "自动识别"
    use_llm_note = "是" if session.config.use_llm else "否"
    return (
        f"请抓取新闻网站：{session.config.entry_url}\n"
        f"计划抓取文章数：{session.config.max_articles} 篇\n"
        f"语言提示：{language}\n"
        f"当前站点画像：{profile_name}\n"
        f"浏览器模式：{session.config.browser_mode}（是否允许使用 LLM：{use_llm_note}）\n"
        "\n请开始：调用 crawl_site 抓取，随后用 analyze_coverage 自检；"
        "不足时 inspect_site 探查结构并 save_site_profile 补抓；最后调用 save_outputs 输出中文报告。"
    )


def _agent_loop(session: AgentSession, client: Any, llm: dict[str, Any]) -> str:
    """标准 Function Calling 循环：模型决策 → execute_tool → 结果回传模型。"""
    config = session.config
    max_rounds = max(1, _as_int(getattr(config, "max_rounds", 12), 12))
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _user_prompt(session)},
    ]
    final_answer = ""
    for step in range(max_rounds):
        session.emit(f"第 {step + 1}/{max_rounds} 轮：等待模型决策……", "planning")
        response = client.chat.completions.create(
            model=llm.get("model") or "deepseek-flash",
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=float(llm.get("temperature", 0.2)),
            stream=False,
        )
        choices = list(getattr(response, "choices", []) or [])
        if not choices:
            raise NewsAgentError("LLM 没有返回任何候选回复")
        message = choices[0].message
        messages.append(message.model_dump(exclude_none=True))
        tool_calls = list(getattr(message, "tool_calls", None) or [])
        if not tool_calls:
            final_answer = str(getattr(message, "content", "") or "").strip()
            break

        turn_trace: dict[str, Any] = {"round": step + 1, "calls": []}
        session.trace.append(turn_trace)
        for call in tool_calls:
            function = getattr(call, "function", None)
            tool_name = str(getattr(function, "name", "") or "")
            raw_arguments = getattr(function, "arguments", "") or "{}"
            session.emit(f"第 {step + 1} 轮：模型调用工具 {tool_name}", "planning")
            try:
                tool_result = execute_tool(tool_name, raw_arguments, session)
            except Exception as error:  # execute_tool 已兜底，这里再保一层
                tool_result = {"error": f"工具执行失败：{type(error).__name__}: {error}"}
            turn_trace["calls"].append({
                "tool": tool_name,
                "arguments": _parse_tool_arguments(raw_arguments),
                "result": tool_result,
            })
            messages.append({
                "role": "tool",
                "tool_call_id": str(getattr(call, "id", "") or ""),
                "content": json.dumps(tool_result, ensure_ascii=False, default=str),
            })
            result_text = _tool_result_line(tool_result)
            session.emit(f"工具 {tool_name} 返回：{result_text}", "planning")
    else:
        final_answer = f"已达到最大决策轮次（{max_rounds} 轮），Agent 结束。"

    return final_answer


def _tool_result_line(result: dict[str, Any]) -> str:
    """把工具结果压成一行进度日志（顺序很重要：save_outputs 也带 article_count）。"""
    if not isinstance(result, dict):
        return _truncate(result, 100)
    if result.get("error"):
        return "错误：" + _truncate(result["error"], 120)
    if "saved" in result:
        if result.get("saved"):
            return f"已保存：{_truncate(result.get('output_dir', ''), 100)}"
        return "保存未完成"
    if "added" in result:
        return (
            f"新增 {result.get('added', 0)} 篇，累计 {result.get('total_articles', 0)}/"
            f"{result.get('target', 0)} 篇"
        )
    if "article_count" in result:
        rates = result.get("completeness") or {}
        title = rates.get("title") if isinstance(rates, dict) else None
        if isinstance(title, dict) and "rate" in title:
            return f"{result.get('article_count', 0)} 篇，标题完整率 {float(title['rate']) * 100:.0f}%"
        return f"{result.get('article_count', 0)} 篇"
    return _truncate(json.dumps(result, ensure_ascii=False), 100)


def run_agent(
    config: CrawlConfig,
    settings: dict[str, Any] | None = None,
    on_event: Callable[..., Any] | None = None,
    intervention_event: Any = None,
) -> tuple[str, CrawlResult]:
    """DeepSeek Agent Loop 入口；无 Key / API 失败时自动降级为确定性流水线。"""
    settings = settings if settings is not None else config_module.load_settings()
    config_module.load_local_env()
    llm = config_module.get_llm_config(settings)

    if not config.use_llm:
        _emit(on_event, "已指定不使用 LLM，改用确定性流水线。", "planning")
        result = _run_deterministic_core(
            config,
            settings,
            on_event,
            intervention_event,
            note="本次运行指定不使用 LLM，由确定性流水线完成采集与模板报告。",
        )
        answer = f"已使用确定性流水线抓取 {len(result.articles)} 篇文章。"
        return answer, result
    if not llm.get("api_key"):
        _emit(on_event, "未检测到 DEEPSEEK_API_KEY，改用确定性流水线。", "planning")
        result = _run_deterministic_core(
            config,
            settings,
            on_event,
            intervention_event,
            note="本次运行未检测到 DEEPSEEK_API_KEY，未使用 LLM，由确定性流水线完成采集与模板报告。",
        )
        answer = f"未配置 DEEPSEEK_API_KEY，已使用确定性流水线抓取 {len(result.articles)} 篇文章。"
        return answer, result

    _emit(on_event, "DeepSeek 正在规划抓取步骤……", "planning")
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=str(llm["api_key"]),
            base_url=str(llm.get("base_url") or "https://api.deepseek.com"),
            timeout=90.0,
            max_retries=1,
        )
    except Exception as error:
        message = f"{type(error).__name__}: {error}"
        _emit(on_event, f"LLM 客户端初始化失败（{message}），改用确定性流水线。", "failed")
        result = _run_deterministic_core(
            config,
            settings,
            on_event,
            intervention_event,
            note=f"LLM 客户端初始化失败（{message}），已降级为确定性流水线。",
        )
        return f"LLM 不可用，已使用确定性流水线抓取 {len(result.articles)} 篇文章。", result

    session = AgentSession(config, settings, on_event, intervention_event)
    final_answer = ""
    try:
        session.load_profile()
        final_answer = _agent_loop(session, client, llm)
        if not session.result.articles:
            _emit(on_event, "没有抽取到文章，执行一次确定性补抓……", "extracting")
            try:
                session.ensure_discovery()
                session.crawl(limit=config.max_articles)
            except Exception as error:
                session.stats.add_warning(f"补抓失败：{type(error).__name__}: {error}")
                _emit(on_event, f"补抓失败：{error}", "extracting")
        need_save = (
            not session.has_outputs
            or session.saved_article_count != len(session.result.articles)
        )
        if need_save:
            report = session.report_markdown_used or final_answer
            session.save_outputs(report or None)
        session.refresh_diagram()
        session.stats.finish()
    except Exception as error:  # noqa: BLE001 - LLM/工具/网络异常都要有兜底产物
        message = f"{type(error).__name__}: {error}"
        session.stats.add_error(config.entry_url, "agent", message)
        session.stats.add_warning(f"LLM Agent 运行失败：{message}")
        _emit(on_event, f"Agent 循环失败：{message}", "failed")
        if session.result.articles or session.has_outputs:
            try:
                base_report = session.report_markdown_used or final_answer
                if base_report:
                    fallback = _report_with_note(
                        base_report,
                        f"LLM Agent 运行中断（{message}），报告由兜底逻辑保存。",
                    )
                else:
                    fallback = _template_report(
                        session.result,
                        f"LLM Agent 运行中断（{message}），已保存确定性采集结果。",
                    )
                session.stats.finish()
                session.save_outputs(fallback)
                session.refresh_diagram()
            except Exception as save_error:  # noqa: BLE001
                session.stats.add_warning(f"兜底输出保存失败：{type(save_error).__name__}: {save_error}")
        else:
            session.close()
            result = _run_deterministic_core(
                config,
                settings,
                on_event,
                intervention_event,
                note=f"LLM Agent 运行失败（{message}），已降级为确定性流水线。",
            )
            return f"LLM 运行失败，已降级为确定性流水线抓取 {len(result.articles)} 篇文章。", result
    finally:
        session.stats.finish()
        session.close()

    result = session.result
    if not final_answer:
        if result.articles:
            final_answer = (
                f"已完成 {len(result.articles)} 篇文章的抓取，"
                f"结构化数据与报告见输出目录：{result.output_files.get('dir', '')}"
            )
        else:
            final_answer = "本次未抓取到文章，请查看报告与 run_meta.json 中的错误信息。"
    _emit(
        on_event,
        f"Agent 抓取完成：{len(result.articles)} 篇文章，输出目录 {result.output_files.get('dir', '')}",
        "completed",
    )
    return final_answer, result
