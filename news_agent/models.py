"""新闻抓取 Agent 的数据模型。

跨模块传递的数据统一使用这里的 dataclass；to_dict() 保证
JSONL / CSV / run_meta.json 的字段名长期稳定（SCHEMA_VERSION）。
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .urls import domain_from_url, normalize_entry_url

SCHEMA_VERSION = "1.0"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 NewsAgent/1.0"
)

# 站点配置（SiteProfile）中各分组的默认值。配置缺省时逐层深合并。
DEFAULT_FETCH = {"mode": "auto", "encoding": "auto", "wait_selector": ""}
DEFAULT_DISCOVERY = {
    "feeds": [],
    "use_jsonld": True,
    "use_heuristics": True,
    "max_scroll": 0,
}
DEFAULT_LIST = {
    "url": "",
    "item": "",
    "item_title": "",
    "item_date": "",
    "item_section": "",
    "item_image": "",
    "pagination": {"next": "", "max_pages": 3},
}
DEFAULT_ARTICLE = {
    "title": "",
    "body": "",
    "date": "",
    "author": "",
    "source": "",
    "summary": "",
    "section": "",
    "exclude": [],
}

ARTICLE_FIELDS = (
    "id",
    "url",
    "canonical_url",
    "title",
    "summary",
    "body",
    "published_at",
    "published_raw",
    "author",
    "source",
    "section",
    "language",
    "site_domain",
    "images",
    "crawled_at",
    "extract_method",
    "confidence",
    "word_count",
    "list_page",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def content_id(url: str) -> str:
    """按 URL 生成 16 位稳定 id，用于 JSONL / CSV 主键与去重。"""
    return hashlib.sha1(url.strip().encode("utf-8", "ignore")).hexdigest()[:16]


def _deep_merge(defaults: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(defaults)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


@dataclass
class NewsItem:
    """新闻列表页中的一个候选项（尚未抓正文）。"""

    url: str
    title: str = ""
    published_raw: str = ""
    section: str = ""
    image: str = ""
    list_url: str = ""
    method: str = "generic"  # profile | feed | jsonld | heuristic | sitemap | llm

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Article:
    """一篇已抽取的新闻文章，字段即结构化输出 schema。"""

    url: str
    canonical_url: str = ""
    title: str = ""
    summary: str = ""
    body: str = ""
    published_at: str = ""  # ISO 8601（含时区），未知为空字符串
    published_raw: str = ""
    author: str = ""
    source: str = ""
    section: str = ""
    language: str = ""
    site_domain: str = ""
    images: list[str] = field(default_factory=list)
    crawled_at: str = ""
    extract_method: str = ""
    confidence: float = 0.0
    word_count: int = 0
    list_page: str = ""

    def __post_init__(self) -> None:
        if not self.crawled_at:
            self.crawled_at = utc_now_iso()
        if not self.site_domain:
            self.site_domain = domain_from_url(self.url)
        if not self.canonical_url:
            self.canonical_url = self.url
        if self.body and not self.word_count:
            self.word_count = len(self.body)

    @property
    def id(self) -> str:
        return content_id(self.canonical_url or self.url)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["id"] = self.id
        return {key: data[key] for key in ARTICLE_FIELDS}

    def is_usable(self, min_body_chars: int = 120) -> bool:
        """是否达到“可作为结构化新闻输出”的最低要求。"""
        return bool(self.title) and len(self.body) >= max(1, min_body_chars)

    def completeness(self) -> dict[str, bool]:
        return {
            "title": bool(self.title),
            "body": bool(self.body),
            "published_at": bool(self.published_at),
            "author": bool(self.author),
            "source": bool(self.source),
            "language": bool(self.language),
        }


@dataclass
class SiteProfile:
    """站点级抓取配置（sites/<domain>.json）。

    选择器语法见 news_agent/dom.py：
    - CSS 子集：`article h2, h1`、`a.headline[href]`
    - XPath：`xpath://article//h1`
    - 取值后缀：`::attr(href)`、`::text`
    """

    domain: str
    name: str = ""
    language: str = ""
    fetch: dict[str, Any] = field(default_factory=lambda: deepcopy(DEFAULT_FETCH))
    discovery: dict[str, Any] = field(default_factory=lambda: deepcopy(DEFAULT_DISCOVERY))
    list: dict[str, Any] = field(default_factory=lambda: deepcopy(DEFAULT_LIST))
    article: dict[str, Any] = field(default_factory=lambda: deepcopy(DEFAULT_ARTICLE))
    # 注意：这里不能用 default_factory=list —— 类体前面定义了名为 list 的字段，
    # 会遮蔽内置 list，导致 dataclass 拿到 Field 对象。用 lambda 显式返回空列表。
    include: list[str] = field(default_factory=lambda: [])
    exclude: list[str] = field(default_factory=lambda: [])
    note: str = ""
    learned_by: str = ""  # human | agent
    updated_at: str = ""

    def __post_init__(self) -> None:
        self.fetch = _deep_merge(DEFAULT_FETCH, self.fetch)
        self.discovery = _deep_merge(DEFAULT_DISCOVERY, self.discovery)
        self.list = _deep_merge(DEFAULT_LIST, self.list)
        self.article = _deep_merge(DEFAULT_ARTICLE, self.article)
        self.domain = str(self.domain or "").strip().lower()
        if self.domain.startswith("www."):
            self.domain = self.domain[4:]
        if not self.domain:
            raise ValueError("站点配置缺少 domain")
        if not self.updated_at:
            self.updated_at = utc_now_iso()

    def group(self, name: str) -> dict[str, Any]:
        return getattr(self, name, {})

    def selector(self, group: str, key: str) -> str:
        value = self.group(group).get(key, "")
        return value if isinstance(value, str) else ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SiteProfile":
        if not isinstance(data, dict):
            raise ValueError("站点配置必须是 JSON 对象")
        known = {key: data.get(key) for key in (
            "domain", "name", "language", "fetch", "discovery", "list",
            "article", "include", "exclude", "note", "learned_by", "updated_at",
        ) if key in data}
        return cls(**known)


@dataclass
class CrawlConfig:
    """一次抓取任务的运行配置（settings.json + CLI/网页参数合并）。"""

    entry_url: str
    max_articles: int = 10
    max_list_pages: int = 3
    max_candidates: int = 150
    timeout_s: float = 15.0
    retries: int = 2
    min_delay_s: float = 0.8
    respect_robots: bool = True
    browser_mode: str = "auto"  # auto | always | never
    browser_wait_ms: int = 2500
    headless: bool = True
    max_body_chars: int = 20000
    default_timezone: str = "UTC"
    language_hint: str = ""
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    use_llm: bool = True
    max_rounds: int = 12
    user_agent: str = DEFAULT_USER_AGENT
    out_dir: str = "data"

    def __post_init__(self) -> None:
        self.entry_url = normalize_entry_url(self.entry_url)
        self.max_articles = max(1, int(self.max_articles))
        self.max_list_pages = max(1, int(self.max_list_pages))
        self.max_candidates = max(self.max_articles, int(self.max_candidates))
        self.browser_mode = str(self.browser_mode or "auto").lower()
        if self.browser_mode not in ("auto", "always", "never"):
            self.browser_mode = "auto"

    @property
    def domain(self) -> str:
        return domain_from_url(self.entry_url)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_settings(
        cls,
        entry_url: str,
        settings: dict[str, Any] | None = None,
        **overrides: Any,
    ) -> "CrawlConfig":
        """从 config/settings.json 合并出的 settings 构造配置，overrides 优先。"""
        settings = settings or {}
        crawl = dict(settings.get("crawl") or {})
        known = {
            key: crawl[key]
            for key in (
                "max_articles", "max_list_pages", "max_candidates", "timeout_s",
                "retries", "min_delay_s", "respect_robots", "browser_mode",
                "browser_wait_ms", "headless", "max_body_chars", "default_timezone",
                "language_hint", "include_patterns", "exclude_patterns",
            )
            if key in crawl
        }
        llm = dict(settings.get("llm") or {})
        known["max_rounds"] = int(llm.get("max_rounds", 12))
        known["user_agent"] = str(settings.get("user_agent") or DEFAULT_USER_AGENT)
        known["out_dir"] = str((settings.get("output") or {}).get("dir") or "data")
        known.update({key: value for key, value in overrides.items() if value is not None})
        return cls(entry_url=entry_url, **known)


@dataclass
class CrawlStats:
    """一次任务的运行统计，写入 run_meta.json 并用于 Agent 自检。"""

    pages_fetched: int = 0
    list_pages: int = 0
    candidates: int = 0
    extracted: int = 0
    failed: int = 0
    duplicates: int = 0
    browser_fetches: int = 0
    discovery_methods: dict[str, int] = field(default_factory=dict)
    extract_methods: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str = ""

    def note_method(self, bucket: str, name: str) -> None:
        target = self.discovery_methods if bucket == "discovery" else self.extract_methods
        target[name] = target.get(name, 0) + 1

    def add_error(self, url: str, stage: str, error: str) -> None:
        if len(self.errors) < 50:
            self.errors.append({"url": url, "stage": stage, "error": str(error)[:500]})

    def add_warning(self, message: str) -> None:
        if len(self.warnings) < 50 and message not in self.warnings:
            self.warnings.append(str(message)[:500])

    def finish(self) -> None:
        self.finished_at = utc_now_iso()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CrawlResult:
    """一次抓取任务的完整结果，交给 output.py 落盘。"""

    entry_url: str
    domain: str
    profile_used: str = ""
    discovery_method: str = ""
    feeds: list[str] = field(default_factory=list)
    articles: list[Article] = field(default_factory=list)
    stats: CrawlStats = field(default_factory=CrawlStats)
    report_markdown: str = ""
    output_files: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add_article(self, article: Article) -> None:
        self.articles.append(article)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_url": self.entry_url,
            "domain": self.domain,
            "profile_used": self.profile_used,
            "discovery_method": self.discovery_method,
            "feeds": list(self.feeds),
            "article_count": len(self.articles),
            "stats": self.stats.to_dict(),
            "warnings": list(self.warnings),
            "report_markdown": self.report_markdown,
            "output_files": dict(self.output_files),
            "articles": [article.to_dict() for article in self.articles],
        }
