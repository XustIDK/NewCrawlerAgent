"""结构化输出：JSONL / CSV / run_meta / Markdown 报告 / 抓取流程图。

一次任务在输出根目录下创建 <domain>_<YYYYmmdd-HHMMSS>/ 子目录，产出：

- articles.jsonl：每行一篇 Article（schema v1.0，UTF-8，ensure_ascii=False）
- articles.csv：扁平表格（UTF-8 BOM，Excel 可直接打开）
- run_meta.json：入口、配置、发现方式、统计、错误、警告与输出索引
- report.md：模型报告或模板统计报告，末尾附输出文件清单
- agent_loop.svg / .png：Agent Loop 工具调用轨迹图；无轨迹时画确定性流水线

接口契约见 DESIGN.md 第 4.5、9.7 节。
"""

from __future__ import annotations

import csv
import json
import re
import textwrap
from collections import Counter
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Callable

from .config import resolve_output_dir
from .errors import NewsAgentError
from .models import SCHEMA_VERSION, CrawlConfig, CrawlResult, utc_now_iso

# articles.csv 的列顺序（与 DESIGN.md 第 4.5 节一致，保持稳定）。
CSV_COLUMNS = (
    "id",
    "url",
    "canonical_url",
    "title",
    "source",
    "author",
    "published_at",
    "language",
    "section",
    "word_count",
    "confidence",
    "extract_method",
    "list_page",
    "body",
)

FIELD_LABELS = {
    "title": "标题",
    "body": "正文",
    "published_at": "发布时间",
    "author": "作者",
    "source": "来源",
    "language": "语言",
}

DISCOVERY_LABELS = {
    "profile": "站点配置",
    "feed": "RSS/Atom",
    "jsonld": "JSON-LD",
    "heuristic": "链接聚类",
    "mixed": "混合策略",
    "sitemap": "站点地图",
    "llm": "模型推断",
}

COMPLETENESS_FIELDS = ("title", "body", "published_at", "author", "source", "language")

# 流程图配色：沿用原项目（RedBookFinal.py）的绿色视觉风格。
SVG_BG = "#f5f7f4"
SVG_DARK = "#284f40"
SVG_ACCENT = "#688477"
SVG_MUTED = "#748078"
SVG_TEXT = "#26342c"
SVG_SOFT = "#edf3ee"
SVG_LINE = "#dbe3dc"
SVG_FONT = "Arial, 'Microsoft YaHei', 'PingFang SC', 'Noto Sans CJK SC', sans-serif"

# PNG 渲染回调：renderer(svg_source, width, height) -> bytes | None
Renderer = Callable[[str, int, int], "bytes | None"]


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _short(text: Any, limit: int = 60) -> str:
    """把任意值压成单行短文本，用于报告/图表摘要。"""
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)] + "…"


def _safe_domain(domain: str) -> str:
    """域名转目录名：只保留字母数字与 ._-，其余替换为下划线。"""
    value = re.sub(r"[^0-9A-Za-z._-]+", "_", str(domain or "").strip())
    value = value.strip("._-")
    return value or "site"


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _unique_output_dir(base_dir: Path, stem: str) -> Path:
    """同一秒内重复运行时追加序号，避免覆盖已有结果。"""
    candidate = base_dir / stem
    if not candidate.exists():
        return candidate
    for index in range(2, 1000):
        candidate = base_dir / f"{stem}-{index}"
        if not candidate.exists():
            return candidate
    return base_dir / f"{stem}-{datetime.now().microsecond}"


def _write_text(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8", newline="\n")
    except OSError as error:
        raise NewsAgentError(f"写入文件失败：{path} —— {error}") from error


def _write_bytes(path: Path, data: bytes) -> None:
    try:
        path.write_bytes(data)
    except OSError as error:
        raise NewsAgentError(f"写入文件失败：{path} —— {error}") from error


def _format_rate(value: float) -> str:
    return f"{float(value or 0.0) * 100:.0f}%"


def _discovery_label(method: str) -> str:
    return DISCOVERY_LABELS.get(str(method or ""), str(method or "未记录"))


def _merge_warnings(result: CrawlResult) -> list[str]:
    merged: list[str] = []
    for item in list(result.warnings) + list(result.stats.warnings):
        text = str(item or "").strip()
        if text and text not in merged:
            merged.append(text)
    return merged


# ---------------------------------------------------------------------------
# 字段统计
# ---------------------------------------------------------------------------

def _field_completeness(articles: list[Any]) -> dict[str, dict[str, Any]]:
    total = len(articles)
    completeness: dict[str, dict[str, Any]] = {}
    for field in COMPLETENESS_FIELDS:
        count = sum(1 for article in articles if getattr(article, field, ""))
        completeness[field] = {
            "count": count,
            "rate": round(count / total, 4) if total else 0.0,
        }
    return completeness


def _language_distribution(articles: list[Any]) -> list[tuple[str, int]]:
    counter = Counter(str(getattr(article, "language", "") or "未知") for article in articles)
    return counter.most_common()


def _date_span(articles: list[Any]) -> dict[str, Any]:
    dates = sorted(
        str(getattr(article, "published_at", "") or "")[:10]
        for article in articles
        if re.match(r"^\d{4}-\d{2}-\d{2}", str(getattr(article, "published_at", "") or ""))
    )
    unique = sorted(set(dates))
    if not unique:
        return {"earliest": "", "latest": "", "days": 0, "with_date": 0}
    try:
        earliest = datetime.strptime(unique[0], "%Y-%m-%d")
        latest = datetime.strptime(unique[-1], "%Y-%m-%d")
        days = (latest - earliest).days + 1
    except ValueError:
        days = 0
    return {
        "earliest": unique[0],
        "latest": unique[-1],
        "days": days,
        "with_date": len(dates),
    }


# ---------------------------------------------------------------------------
# 模板报告
# ---------------------------------------------------------------------------

def _md_cell(value: Any) -> str:
    return " ".join(str(value or "").replace("|", "\\|").split())


def build_template_report(result: CrawlResult) -> str:
    """生成无 LLM 时的中文模板统计报告（事实均来自 CrawlResult）。"""
    articles = list(result.articles)
    stats = result.stats
    completeness = _field_completeness(articles)
    languages = _language_distribution(articles)
    span = _date_span(articles)
    warnings = _merge_warnings(result)

    lines: list[str] = [
        f"# 新闻抓取报告：{_short(result.domain or result.entry_url, 80)}",
        "",
        f"- 入口网址：{result.entry_url}",
        f"- 生成时间：{utc_now_iso()}",
        f"- 发现方式：{_discovery_label(result.discovery_method)}",
        "- 站点画像："
        + (f"已使用 {result.profile_used}" if result.profile_used else "未使用（通用策略）"),
        f"- 抓取文章：{len(articles)} 篇",
    ]
    if result.feeds:
        lines.append("- RSS/Atom：" + "、".join(_short(feed, 100) for feed in result.feeds[:5]))
    lines += ["", "## 采集概况", "", "| 指标 | 数值 |", "| --- | --- |"]
    lines.extend(
        [
            f"| 候选链接 | {stats.candidates} |",
            f"| 列表页数 | {stats.list_pages} |",
            f"| 抽取成功 | {stats.extracted or len(articles)} |",
            f"| 抽取失败 | {stats.failed} |",
            f"| 重复链接 | {stats.duplicates} |",
            f"| 浏览器抓取 | {stats.browser_fetches} |",
        ]
    )
    if stats.discovery_methods:
        method_text = "、".join(
            f"{_discovery_label(name)}×{count}" for name, count in stats.discovery_methods.items()
        )
        lines.append(f"| 发现方式明细 | {_md_cell(method_text)} |")
    if stats.extract_methods:
        extract_text = "、".join(
            f"{name}×{count}" for name, count in stats.extract_methods.items()
        )
        lines.append(f"| 抽取策略明细 | {_md_cell(extract_text)} |")

    lines += ["", "## 字段完整率", "", "| 字段 | 有值数 | 完整率 |", "| --- | --- | --- |"]
    for field in COMPLETENESS_FIELDS:
        data = completeness[field]
        lines.append(
            f"| {FIELD_LABELS[field]} | {data['count']}/{len(articles)} | {_format_rate(data['rate'])} |"
        )

    lines += ["", "## 语言分布", ""]
    if languages:
        lines += ["| 语言 | 篇数 |", "| --- | --- |"]
        for language, count in languages:
            lines.append(f"| {_md_cell(language)} | {count} |")
    else:
        lines.append("暂无可统计的语言数据。")

    lines += ["", "## 发布时间范围", ""]
    if span["earliest"]:
        lines.append(
            f"- 最早：{span['earliest']}；最新：{span['latest']}；跨 {span['days']} 天"
            f"（共 {span['with_date']}/{len(articles)} 篇有可用时间）"
        )
    else:
        lines.append("未解析到有效的发布时间（页面可能缺少时间字段或使用了脚本渲染）。")

    lines += ["", "## 文章列表", ""]
    if articles:
        lines += ["| # | 标题 | 来源 | 发布时间 | 链接 |", "| --- | --- | --- | --- | --- |"]
        for index, article in enumerate(articles, start=1):
            title = _md_cell(_short(getattr(article, "title", "") or "（无标题）", 120))
            title_link = f"[{title}]({getattr(article, 'url', '')})"
            source = _md_cell(_short(getattr(article, "source", "") or "未识别", 40))
            published = _md_cell(str(getattr(article, "published_at", "") or "未知")[:19])
            lines.append(
                f"| {index} | {title_link} | {source} | {published} | {getattr(article, 'url', '')} |"
            )
    else:
        lines.append("本次没有抽取到文章。请查看下方采集问题与 run_meta.json 中的错误信息。")

    lines += ["", "## 采集问题", ""]
    issue_lines: list[str] = []
    for warning in warnings[:10]:
        issue_lines.append(f"- 警告：{_short(warning, 200)}")
    for error in stats.errors[:10]:
        issue_lines.append(
            f"- 错误（{_short(error.get('stage', ''), 20)}）：{_short(error.get('url', ''), 90)}"
            f" —— {_short(error.get('error', ''), 180)}"
        )
    lines.extend(issue_lines or ["- 本次运行没有记录到警告或错误。"])

    lines += [
        "",
        "## 说明",
        "",
        "- 本报告由确定性流水线基于真实抓取结果自动生成，未使用 LLM 归纳。",
        "- 数据仅来自目标网站的公开页面，请遵守站点服务条款与版权要求，仅用于个人研究。",
        "- 字段完整率反映站点结构差异；正文为页面文本抽取结果，可能包含少量推荐语等噪声，请以原文链接为准。",
        "",
        f"> 数据集：articles.jsonl / articles.csv；运行详情：run_meta.json。本报告生成于 {utc_now_iso()}。",
    ]
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# SVG 基础绘制
# ---------------------------------------------------------------------------

def _svg_lines(text: Any, width: int, max_lines: int) -> list[str]:
    value = " ".join(str(text or "").replace("\n", " ").split())
    if not value:
        return ["（无内容）"]
    lines = textwrap.wrap(value, width=width, break_long_words=True, break_on_hyphens=False)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: max(1, width - 1)] + "…"
    return lines


def _svg_text(
    x: int | float,
    y: int | float,
    text: Any,
    width: int = 28,
    max_lines: int = 3,
    size: int = 15,
    color: str = "#38443e",
    weight: str = "400",
) -> str:
    spans: list[str] = []
    for index, line in enumerate(_svg_lines(text, width, max_lines)):
        dy = "0" if index == 0 else "21"
        spans.append(f'<tspan x="{x}" dy="{dy}">{escape(line)}</tspan>')
    return (
        f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" '
        f'fill="{color}">{"".join(spans)}</text>'
    )


def _svg_header(
    title: str,
    subtitle_line1: str,
    subtitle_line2: str,
    bar_text: str,
    width: int,
) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="__HEIGHT__" '
        f'viewBox="0 0 {width} __HEIGHT__" font-family="{SVG_FONT}">',
        '<defs><marker id="arrow" markerWidth="10" markerHeight="8" refX="8" refY="4" '
        'orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#688477"/></marker></defs>',
        f'<rect width="{width}" height="__HEIGHT__" fill="{SVG_BG}"/>',
        f'<text x="48" y="55" font-size="15" font-weight="700" letter-spacing="1.5" '
        f'fill="#587264">AGENT RUN DIAGRAM</text>',
        f'<text x="48" y="100" font-size="29" font-weight="700" fill="#202a24">{escape(title)}</text>',
        _svg_text(48, 133, subtitle_line1, 72, 2, 16, "#57645c"),
        f'<text x="48" y="183" font-size="14" fill="#718077">{escape(subtitle_line2)}</text>',
        f'<rect x="48" y="207" width="{width - 96}" height="45" rx="10" fill="#e8eee9"/>',
        f'<text x="66" y="235" font-size="14" fill="#40564a">{escape(bar_text)}</text>',
    ]


def _finalize_svg(parts: list[str], width: int, height: int, footer: str) -> tuple[str, int, int]:
    content = "\n".join(parts)
    content = content.replace("__HEIGHT__", str(height))
    footer_y = height - 53
    content += (
        f'\n<text x="48" y="{footer_y}" font-size="13" fill="{SVG_MUTED}">{escape(footer)}</text>'
        "\n</svg>"
    )
    return content, width, height


# ---------------------------------------------------------------------------
# 确定性流水线图（无 trace 时）
# ---------------------------------------------------------------------------

def _build_pipeline_svg(result: CrawlResult) -> tuple[str, int, int]:
    width = 1240
    top = 250
    card_h = 210
    card_w = 244
    step = 300
    articles = list(result.articles)
    stats = result.stats

    stages = [
        ("01", "入口与画像", [
            f"域名：{_short(result.domain or result.entry_url, 30)}",
            f"画像：{_short(result.profile_used, 28) if result.profile_used else '通用策略'}",
            f"列表页：{stats.list_pages} 页",
        ]),
        ("02", "新闻列表发现", [
            f"方式：{_discovery_label(result.discovery_method)}",
            f"候选：{stats.candidates} 个",
            f"feeds：{len(result.feeds)} 个" if result.feeds else "feeds：未使用",
        ]),
        ("03", "文章抽取", [
            f"成功：{len(articles)} 篇",
            f"失败：{stats.failed} 篇",
            "策略：" + ("、".join(list(stats.extract_methods)[:3]) or "通用算法"),
        ]),
        ("04", "结构化输出", [
            "articles.jsonl",
            "articles.csv",
            "run_meta.json / report.md",
            "agent_loop.svg / .png",
        ]),
    ]

    parts = _svg_header(
        "通用新闻抓取流水线",
        f"目标站点：{result.entry_url}",
        f"本次采集 {len(articles)} 篇 · 发现候选 {stats.candidates} 个 · 发现方式：{_discovery_label(result.discovery_method)}",
        "确定性流水线（未使用 LLM 或模型不可用）：入口校验与画像加载 → 新闻列表发现 → 文章抽取 → 结构化输出",
        width,
    )

    for index, (number, title, lines) in enumerate(stages):
        x = 48 + index * step
        parts.append(
            f'<rect x="{x}" y="{top}" width="{card_w}" height="{card_h}" rx="14" '
            f'fill="#ffffff" stroke="{SVG_LINE}"/>'
        )
        parts.append(
            f'<rect x="{x}" y="{top}" width="{card_w}" height="6" rx="3" fill="{SVG_DARK}"/>'
        )
        parts.append(
            f'<text x="{x + 20}" y="{top + 42}" font-size="13" font-weight="700" '
            f'fill="#688477">{number} / {escape(title)}</text>'
        )
        for line_index, line in enumerate(lines):
            parts.append(_svg_text(x + 20, top + 84 + line_index * 34, line, 22, 2, 14, SVG_TEXT))
        if index < len(stages) - 1:
            parts.append(
                f'<path d="M {x + card_w + 6} {top + card_h // 2} L {x + step - 12} '
                f'{top + card_h // 2}" stroke="{SVG_ACCENT}" stroke-width="2" marker-end="url(#arrow)"/>'
            )

    # 底部面板：字段完整率 + 文章样例（或错误摘要）
    panel_y = top + card_h + 70
    completeness = _field_completeness(articles)
    content: list[dict[str, Any]] = []
    if articles:
        for index, article in enumerate(articles[:5], start=1):
            title = _short(getattr(article, "title", "") or "（无标题）", 80)
            content.append({"kind": "title", "text": f"{index}. {title}", "h": 24})
            detail = (
                f"{_short(getattr(article, 'source', '') or '来源未知', 24)} · "
                f"{_short(str(getattr(article, 'published_at', '') or '发布时间未知')[:19], 19)} · "
                f"{_short(getattr(article, 'url', ''), 88)}"
            )
            content.append({"kind": "detail", "text": detail, "h": 22})
    else:
        content.append({
            "kind": "title",
            "text": "本次没有抽取到文章：请查看 errors / warnings，并考虑补充站点画像或换用浏览器模式。",
            "h": 24,
        })
        for error in stats.errors[:3]:
            content.append({
                "kind": "detail",
                "text": f"[{_short(error.get('stage', ''), 16)}] {_short(error.get('error', ''), 100)}",
                "h": 22,
            })
    panel_h = 96 + sum(item["h"] for item in content) + 26
    parts.append(
        f'<rect x="48" y="{panel_y}" width="{width - 96}" height="{panel_h}" rx="14" '
        f'fill="{SVG_SOFT}" stroke="#d3e0d6"/>'
    )
    parts.append(
        _svg_text(68, panel_y + 32, "字段完整率与文章样例", 40, 1, 15, SVG_DARK, "700")
    )
    rate_text = " · ".join(
        f"{FIELD_LABELS[field]} {_format_rate(completeness[field]['rate'])}"
        for field in COMPLETENESS_FIELDS
    )
    parts.append(_svg_text(68, panel_y + 60, rate_text, 80, 1, 13, "#40564a"))
    line_y = panel_y + 92
    for item in content:
        if item["kind"] == "title":
            parts.append(_svg_text(68, line_y, item["text"], 92, 1, 13, SVG_TEXT, "600"))
        else:
            parts.append(_svg_text(82, line_y, item["text"], 120, 1, 11, "#557360"))
        line_y += item["h"]

    height = panel_y + panel_h + 96
    footer = "本图来自确定性流水线；运行中的警告与错误完整记录在 run_meta.json。"
    return _finalize_svg(parts, width, height, footer)


# ---------------------------------------------------------------------------
# Agent Loop 轨迹图（有 trace 时）
# ---------------------------------------------------------------------------

def _trace_tool_call_line(call: dict[str, Any]) -> str:
    name = str(call.get("tool") or "未知工具")
    arguments = call.get("arguments") or {}
    if not isinstance(arguments, dict):
        arguments = {}
    if name == "crawl_site":
        return f"crawl_site(url={_short(arguments.get('url') or '当前站点', 26)}, limit={arguments.get('limit', '默认')})"
    if name == "inspect_site":
        return f"inspect_site(use_browser={bool(arguments.get('use_browser'))})"
    if name == "save_site_profile":
        profile = arguments.get("profile") or {}
        domain = profile.get("domain") if isinstance(profile, dict) else ""
        return f"save_site_profile(domain={_short(domain or '当前站点', 26)})"
    if name == "analyze_coverage":
        return "analyze_coverage() · 自检字段完整率"
    if name == "save_outputs":
        return "save_outputs() · 写入数据集与报告"
    return name


def _trace_result_lines(call: dict[str, Any], target_hint: int) -> list[str]:
    name = str(call.get("tool") or "")
    result = call.get("result") or {}
    if not isinstance(result, dict):
        return [_short(result, 70)]
    if result.get("error"):
        return [f"工具返回错误：{_short(result['error'], 70)}"]
    if name == "crawl_site":
        lines = [
            f"本轮新增 {result.get('added', 0)} 篇；累计 {result.get('total_articles', 0)}/"
            f"{result.get('target', target_hint)} 篇"
        ]
        samples = result.get("samples") or []
        titles = [
            str(item.get("title") or "")
            for item in samples
            if isinstance(item, dict) and item.get("title")
        ]
        if titles:
            lines.append("返回文章：" + "；".join(_short(title, 24) for title in titles[:2]))
        elif not result.get("added"):
            lines.append("没有新增文章")
        if result.get("remaining_candidates") is not None:
            lines.append(f"剩余候选 {result.get('remaining_candidates')} 个")
        missing = result.get("missing_hint")
        if missing:
            lines.append(_short(missing, 60))
        return lines
    if name == "inspect_site":
        lines: list[str] = []
        feeds = result.get("feeds") or []
        if feeds:
            lines.append("发现 RSS：" + "；".join(_short(str(feed), 22) for feed in list(feeds)[:2]))
        types = result.get("jsonld_types") or []
        lines.append(
            "JSON-LD 类型：" + _short("、".join(map(str, list(types)[:4])) or "未发现", 48)
        )
        lines.append(f"候选文章链接样例 {len(result.get('article_links_sample') or [])} 条")
        return lines
    if name == "save_site_profile":
        if result.get("saved"):
            return [f"站点配置已保存：{_short(result.get('path') or '', 58)}"]
        return ["站点配置未保存"]
    if name == "analyze_coverage":
        completeness = result.get("completeness") or {}
        rates = [
            f"{FIELD_LABELS.get(field, field)} {_format_rate(data.get('rate', 0))}"
            for field, data in completeness.items()
            if isinstance(data, dict)
        ]
        lines = [f"已抽取 {result.get('article_count', 0)}/{result.get('target', target_hint)} 篇"]
        if rates:
            lines.append("完整率：" + " · ".join(rates[:3]))
        if len(rates) > 3:
            lines.append(" · ".join(rates[3:]))
        suggestions = result.get("suggestions") or []
        if suggestions:
            lines.append(_short(str(suggestions[0]), 62))
        return lines
    if name == "save_outputs":
        files = result.get("files") or {}
        names = [str(value) for value in list(files.values())[:3]]
        return (["已保存输出"] if result.get("saved") else ["输出保存未完成"]) + names[:2]
    return [_short(json.dumps(result, ensure_ascii=False), 68)]


def _build_trace_svg(trace: list[dict[str, Any]], result: CrawlResult) -> tuple[str, int, int]:
    width = 1240
    top = 280
    row_height = 200
    gap = 42
    card_h = 176
    articles = list(result.articles)
    target_hint = len(articles)

    parts = _svg_header(
        "本次新闻抓取的 Agent Loop",
        f"目标站点：{result.entry_url}",
        f"发现候选 {result.stats.candidates} 个 · 抓取 {len(articles)} 篇 · 模型决策轮次 {len(trace)}",
        "循环含义：模型选择工具 → Python 本地执行 → 工具结果回传模型 → 模型据此决定下一步",
        width,
    )

    model_x, model_w = 48, 250
    tool_x, tool_w = 398, 350
    result_x, result_w = 848, 344
    for index, turn in enumerate(trace):
        y = top + index * (row_height + gap)
        calls = turn.get("calls") or []
        if not isinstance(calls, list) or not calls:
            calls = [{"tool": "未知工具", "arguments": {}, "result": {}}]

        call_lines = [_trace_tool_call_line(call) for call in calls if isinstance(call, dict)]
        return_lines: list[str] = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            if len(calls) > 1:
                call_result = call.get("result") or {}
                call_args = call.get("arguments") or {}
                if call.get("tool") == "crawl_site":
                    added = call_result.get("added", 0) if isinstance(call_result, dict) else 0
                    url = call_args.get("url", "") if isinstance(call_args, dict) else ""
                    return_lines.append(f"抓取 {_short(url, 24)}：新增 {added} 篇")
                else:
                    return_lines.extend(_trace_result_lines(call, target_hint))
            else:
                return_lines.extend(_trace_result_lines(call, target_hint))
        return_lines = return_lines[:5] or ["工具已执行，结果已回传模型"]

        parts.extend([
            f'<text x="48" y="{y - 14}" font-size="13" font-weight="700" fill="#78877d">'
            f'LOOP {turn.get("round", index + 1):02d}</text>',
            f'<rect x="{model_x}" y="{y}" width="{model_w}" height="{card_h}" rx="13" fill="{SVG_DARK}"/>',
            _svg_text(model_x + 18, y + 34, "DeepSeek 模型", 24, 1, 16, "#ffffff", "700"),
            _svg_text(model_x + 18, y + 66, "根据任务与上轮结果选择动作", 26, 2, 13, "#dce9e0"),
            f'<path d="M {model_x + model_w + 8} {y + 56} L {tool_x - 12} {y + 56}" '
            f'stroke="{SVG_ACCENT}" stroke-width="2" marker-end="url(#arrow)"/>',
            f'<rect x="{tool_x}" y="{y}" width="{tool_w}" height="{card_h}" rx="13" '
            f'fill="#ffffff" stroke="{SVG_LINE}"/>',
            _svg_text(tool_x + 18, y + 30, "FUNCTION CALLING · 本地工具", 37, 1, 13, "#668071", "700"),
            _svg_text(tool_x + 18, y + 58, " · ".join(call_lines), 24, 5, 12, SVG_TEXT, "600"),
            f'<path d="M {tool_x + tool_w + 8} {y + 56} L {result_x - 12} {y + 56}" '
            f'stroke="{SVG_ACCENT}" stroke-width="2" marker-end="url(#arrow)"/>',
            f'<rect x="{result_x}" y="{y}" width="{result_w}" height="{card_h}" rx="13" '
            f'fill="{SVG_SOFT}" stroke="#d3e0d6"/>',
            _svg_text(result_x + 16, y + 29, "TOOL RESULT", 30, 1, 12, "#557360", "700"),
        ])
        for line_index, line in enumerate(return_lines):
            parts.append(_svg_text(result_x + 16, y + 53 + line_index * 19, line, 24, 1, 12, "#34473b"))
        if index < len(trace) - 1:
            next_y = top + (index + 1) * (row_height + gap)
            parts.extend([
                f'<path d="M {result_x + result_w - 14} {y + card_h} L {result_x + result_w - 14} '
                f'{y + card_h + 17} L 30 {y + card_h + 17} L 30 {next_y + 56} L {model_x - 2} {next_y + 56}" '
                f'fill="none" stroke="{SVG_ACCENT}" stroke-width="2" marker-end="url(#arrow)"/>',
                f'<rect x="430" y="{y + card_h + 5}" width="350" height="24" rx="8" fill="{SVG_BG}"/>',
                _svg_text(442, y + card_h + 22, "工具结果回传，模型继续判断", 36, 1, 12, "#587264", "600"),
            ])

    rows = max(1, len(trace))
    height = top + rows * row_height + max(0, rows - 1) * gap + 80
    footer = "流程记录来自本次实际工具调用；箭头表示数据流，不展示模型隐藏推理。"
    return _finalize_svg(parts, width, height, footer)


# ---------------------------------------------------------------------------
# 对外：流程图与输出落盘
# ---------------------------------------------------------------------------

def save_agent_loop_diagram(
    trace: list[dict[str, Any]] | None,
    result: CrawlResult,
    out_dir: Path | str,
    renderer: Renderer | None = None,
) -> tuple[str, str]:
    """生成抓取流程图。

    - trace 非空：绘制每轮工具调用/结果卡片（Agent Loop）；
    - trace 为空：绘制确定性流水线（发现 → 抽取 → 输出）。

    先写 SVG；renderer 非空且成功返回 bytes 时再写 PNG。
    返回 (svg_path, png_path)，PNG 未生成时为空字符串。
    """
    output_dir = Path(out_dir)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise NewsAgentError(f"无法创建输出目录：{output_dir} —— {error}") from error

    if trace:
        svg_source, width, height = _build_trace_svg(list(trace), result)
    else:
        svg_source, width, height = _build_pipeline_svg(result)

    svg_path = output_dir / "agent_loop.svg"
    png_path = output_dir / "agent_loop.png"
    _write_text(svg_path, svg_source)

    png_result = ""
    if callable(renderer):
        try:
            data = renderer(svg_source, width, height)
            if isinstance(data, (bytes, bytearray)) and data:
                _write_bytes(png_path, bytes(data))
                png_result = str(png_path)
        except Exception:
            # PNG 只是附加产物，失败时保留 SVG 即可，不影响主流程。
            png_result = ""
    return str(svg_path), png_result


def _append_output_list(report: str, files: dict[str, str]) -> str:
    labels = {
        "jsonl": "文章数据（JSONL）",
        "csv": "文章表格（CSV）",
        "meta": "运行元数据（JSON）",
        "report": "本报告（Markdown）",
        "diagram_svg": "抓取流程图（SVG）",
        "diagram_png": "抓取流程图（PNG）",
    }
    lines = ["", "---", "", "## 输出文件", ""]
    for key in ("jsonl", "csv", "meta", "report", "diagram_svg", "diagram_png"):
        value = files.get(key) or ""
        if not value:
            continue
        lines.append(f"- {labels.get(key, key)}：{Path(value).name}")
    lines.append("")
    lines.append("输出目录中的 run_meta.json 记录了本次运行的完整配置、统计与错误信息。")
    return report.rstrip() + "\n" + "\n".join(lines).rstrip() + "\n"


def write_outputs(
    result: CrawlResult,
    config: CrawlConfig,
    settings: dict[str, Any] | None = None,
    report_markdown: str | None = None,
    trace: list[dict[str, Any]] | None = None,
    renderer: Renderer | None = None,
    *,
    output_dir: Path | str | None = None,
) -> dict[str, str]:
    """把一次抓取任务写成完整数据集，返回输出文件路径字典。

    返回键固定为：dir / jsonl / csv / meta / report / diagram_svg / diagram_png
    （diagram_png 未生成时为空字符串）。

    output_dir 为本模块内部复用参数：Agent 多次 save_outputs 时传入上次目录，
    以便覆盖同一份产物而不产生多个时间戳目录。
    """
    if not isinstance(result, CrawlResult):
        raise NewsAgentError("write_outputs 需要 CrawlResult 对象")
    if not isinstance(config, CrawlConfig):
        raise NewsAgentError("write_outputs 需要 CrawlConfig 对象")

    try:
        base_dir = resolve_output_dir(settings, config.out_dir)
    except OSError as error:
        raise NewsAgentError(f"无法创建输出根目录：{error}") from error

    if output_dir:
        out_dir = Path(output_dir)
    else:
        domain = _safe_domain(result.domain or config.domain or "site")
        out_dir = _unique_output_dir(base_dir, f"{domain}_{_timestamp()}")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise NewsAgentError(f"无法创建输出目录：{out_dir} —— {error}") from error

    articles = list(result.articles)
    files: dict[str, str] = {
        "dir": str(out_dir),
        "jsonl": "",
        "csv": "",
        "meta": "",
        "report": "",
        "diagram_svg": "",
        "diagram_png": "",
    }

    # 1) JSONL
    jsonl_path = out_dir / "articles.jsonl"
    lines = []
    for article in articles:
        try:
            data = article.to_dict()
        except Exception as error:  # 单篇坏数据不应导致整份数据集丢失
            result.stats.add_warning(f"文章序列化失败：{type(error).__name__}: {error}")
            continue
        lines.append(json.dumps(data, ensure_ascii=False, default=str))
    _write_text(jsonl_path, "\n".join(lines) + ("\n" if lines else ""))
    files["jsonl"] = str(jsonl_path)

    # 2) CSV（UTF-8 BOM，newline="" 交给 csv 模块处理换行）
    csv_path = out_dir / "articles.csv"
    try:
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(CSV_COLUMNS)
            for article in articles:
                data = article.to_dict()
                writer.writerow([
                    data.get(column, "") if data.get(column) is not None else ""
                    for column in CSV_COLUMNS
                ])
    except OSError as error:
        raise NewsAgentError(f"写入 CSV 失败：{csv_path} —— {error}") from error
    files["csv"] = str(csv_path)

    # 3) 流程图（SVG，可选 PNG）
    svg_path, png_path = save_agent_loop_diagram(trace, result, out_dir, renderer=renderer)
    files["diagram_svg"] = svg_path
    files["diagram_png"] = png_path or ""

    # 4) 报告
    report_path = out_dir / "report.md"
    files["report"] = str(report_path)
    meta_path = out_dir / "run_meta.json"
    files["meta"] = str(meta_path)
    if report_markdown is not None and str(report_markdown).strip():
        report_text = str(report_markdown).strip()
    else:
        report_text = build_template_report(result).strip()
    report_text = _append_output_list(report_text, files)
    _write_text(report_path, report_text)

    # 5) run_meta.json
    if not result.stats.finished_at:
        result.stats.finish()
    warnings = _merge_warnings(result)
    output_index = {
        key: Path(value).name
        for key, value in files.items()
        if key != "dir" and value
    }
    meta = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now_iso(),
        "entry_url": result.entry_url or config.entry_url,
        "domain": result.domain or config.domain,
        "profile_used": result.profile_used,
        "discovery_method": result.discovery_method,
        "feeds": list(result.feeds),
        "config": config.to_dict(),
        "stats": result.stats.to_dict(),
        "warnings": warnings,
        "article_count": len(articles),
        "errors": list(result.stats.errors),
        "trace_rounds": len(trace or []),
        "output_files": output_index,
    }
    _write_text(meta_path, json.dumps(meta, ensure_ascii=False, indent=2, default=str) + "\n")

    result.output_files = dict(files)
    result.report_markdown = report_text
    return files
