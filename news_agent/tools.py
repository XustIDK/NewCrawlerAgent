"""暴露给 DeepSeek Function Calling 的工具 schema 与本地分发。

设计约定见 DESIGN.md 第 4.6、9.6 节：

- 5 个工具：inspect_site / crawl_site / save_site_profile / analyze_coverage / save_outputs；
- 工具返回值必须是 JSON 可序列化的精简摘要，不把整页 HTML 或全文正文塞回模型；
- 参数容错（JSON 字符串 / 类型转换 / 缺省值），工具异常以 {"error": ...} 回传，
  由 Agent Loop 继续下一轮，不中断整个任务。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 工具 schema（OpenAI tools 格式）
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "inspect_site",
            "description": (
                "抓取并探测一个新闻站点页面结构，返回 RSS/Atom 订阅源、JSON-LD 类型、"
                "候选列表选择器、候选文章链接样例和文章页字段探测结果。"
                "当 crawl_site 发现或抽取效果不好时先调用它，再根据结果决定是否写站点配置。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要探测的页面网址，省略时使用当前任务入口网址。",
                    },
                    "use_browser": {
                        "type": "boolean",
                        "description": "是否强制用 Playwright 浏览器渲染页面（适合 JS 站点/懒加载），默认 false。",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_site",
            "description": (
                "对当前新闻站点执行确定性发现 + 抽取，抓取文章正文并返回本轮新增数量、"
                "累计数量、前几篇文章样例、剩余候选与缺失字段提示。可多次调用补抓。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要抓取的新闻列表页网址，省略时使用当前任务入口网址。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "本轮最多新增抓取的文章数，1-50；省略时按剩余目标数量抓取。",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_site_profile",
            "description": (
                "校验并保存站点抓取配置（sites/<domain>.json），随后自动加载新配置。"
                "当 inspect_site 显示了明确的选择器时，用它把配置写入并再次 crawl_site 补抓。"
                "配置对象字段：domain（必填）、name、language、fetch、discovery、list、article、"
                "include、exclude、note。选择器支持 CSS 子集、xpath://...、::attr(name)/::text 后缀。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "object",
                        "description": "站点配置对象，结构参考 sites/_template.json。",
                        "properties": {
                            "domain": {
                                "type": "string",
                                "description": "归一化域名，例如 bbc.com；省略时使用当前任务域名。",
                            },
                            "name": {"type": "string", "description": "站点名称。"},
                            "language": {"type": "string", "description": "站点默认语言，例如 en/zh-CN/ja。"},
                            "fetch": {"type": "object", "description": "抓取模式：mode/encoding/wait_selector。"},
                            "discovery": {"type": "object", "description": "发现配置：feeds/use_jsonld/use_heuristics/max_scroll。"},
                            "list": {
                                "type": "object",
                                "description": (
                                    "列表页配置：url/item/item_title/item_date/item_section/item_image/"
                                    "pagination{next,url_template,max_pages}。"
                                ),
                            },
                            "article": {
                                "type": "object",
                                "description": (
                                    "文章页配置：title/body/date/author/source/summary/section/exclude。"
                                ),
                            },
                            "include": {"type": "array", "items": {"type": "string"}, "description": "文章 URL 正则白名单。"},
                            "exclude": {"type": "array", "items": {"type": "string"}, "description": "文章 URL 正则黑名单。"},
                            "note": {"type": "string", "description": "备注，说明配置依据。"},
                        },
                        "required": ["domain"],
                    },
                },
                "required": ["profile"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_coverage",
            "description": (
                "检查当前抓取覆盖情况：文章数、目标数、各字段完整率、语言分布、发布时间范围、"
                "抽取失败原因、剩余候选和建议的下一步动作。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_outputs",
            "description": (
                "把当前抓取结果保存为结构化数据集并生成中文报告：articles.jsonl、articles.csv、"
                "run_meta.json、report.md 和 agent_loop 流程图。报告必须基于真实抓取内容，"
                "包含采集概况、字段完整率、语言与时间分布、主要文章列表（标题 + 真实链接）、问题与局限。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "report_markdown": {
                        "type": "string",
                        "description": "完整的中文 Markdown 报告；不得编造文章内容，引用的链接必须真实。",
                    },
                },
                "required": ["report_markdown"],
            },
        },
    },
]

TOOL_NAMES = tuple(schema["function"]["name"] for schema in TOOL_SCHEMAS)


# ---------------------------------------------------------------------------
# 参数容错
# ---------------------------------------------------------------------------

def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip() or default
    return str(value).strip() or default


def _as_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on", "是", "需要")


def _parse_arguments(arguments: Any) -> tuple[dict[str, Any], str]:
    """把模型给的参数统一成 dict；返回 (参数, 错误信息)。"""
    if arguments is None:
        return {}, ""
    if isinstance(arguments, dict):
        return dict(arguments), ""
    if isinstance(arguments, (bytes, bytearray)):
        arguments = arguments.decode("utf-8", "ignore")
    if isinstance(arguments, str):
        text = arguments.strip()
        if not text:
            return {}, ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            return {}, f"工具参数不是合法 JSON：{error}"
        if not isinstance(data, dict):
            return {}, "工具参数必须是 JSON 对象"
        return data, ""
    return {}, f"不支持的参数类型：{type(arguments).__name__}"


def _json_safe(value: Any) -> Any:
    """确保返回值可被 json.dumps 序列化（Path/bytes/NaN 等一律转字符串）。"""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _trim_value(value: Any, depth: int = 0) -> Any:
    """压缩结构探测结果：字符串截断、列表/字典限量，避免回传过大。"""
    if isinstance(value, str):
        return value if len(value) <= 300 else value[:299] + "…"
    if isinstance(value, dict):
        items = list(value.items())[:20]
        return {str(key): _trim_value(item, depth + 1) for key, item in items}
    if isinstance(value, (list, tuple)):
        return [_trim_value(item, depth + 1) for item in list(value)[:10]]
    return value


def _short(text: Any, limit: int = 60) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: max(1, limit - 1)] + "…"


# ---------------------------------------------------------------------------
# 各工具实现
# ---------------------------------------------------------------------------

def _tool_inspect_site(arguments: dict[str, Any], session: Any) -> dict[str, Any]:
    url = _as_text(arguments.get("url")) or None
    use_browser = _as_bool(arguments.get("use_browser"))
    data = session.inspect(url, use_browser=use_browser)
    if not isinstance(data, dict):
        return {"error": "结构探测没有返回有效结果，请检查网址是否可访问。"}
    result: dict[str, Any] = {
        "url": _trim_value(data.get("url") or url or ""),
        "title": _trim_value(data.get("title", "")),
        "language": _trim_value(data.get("language", "")),
        "feeds": _trim_value(data.get("feeds") or []),
        "jsonld_types": _trim_value(data.get("jsonld_types") or []),
        "candidate_selectors": _trim_value(data.get("candidate_selectors") or []),
        "article_links_sample": _trim_value(data.get("article_links_sample") or []),
        "pagination_hint": _trim_value(data.get("pagination_hint", "")),
        "article_page_probe": _trim_value(data.get("article_page_probe") or {}),
    }
    # 保留 discover.probe_structure 的其他字段（截断防爆），不覆盖已整理内容。
    for key, value in data.items():
        if key == "error" or key in result:
            continue
        result[key] = _trim_value(value)
    error_text = _trim_value(data.get("error") or "")
    if error_text:
        result["error"] = error_text
    result["hint"] = (
        "若候选选择器或文章页字段清晰，可调用 save_site_profile 写入站点配置后再次 crawl_site 补抓；"
        "若页面是 JS 渲染，可尝试 use_browser=true 再探测。"
    )
    return result


def _tool_crawl_site(arguments: dict[str, Any], session: Any) -> dict[str, Any]:
    url = _as_text(arguments.get("url")) or None
    limit = _as_int(arguments.get("limit"))
    if limit is not None and limit <= 0:
        return {"error": "limit 必须是正整数（1-50）。"}
    if limit is not None:
        limit = min(limit, 50)
    summary = session.crawl(limit=limit, url=url)
    if not isinstance(summary, dict):
        return {"error": "抓取没有返回有效摘要。"}
    result = dict(summary)
    config = getattr(session, "config", None)
    target = int(getattr(config, "max_articles", 0) or 0)
    result.setdefault("target", target)
    result.setdefault("total_articles", 0)
    result.setdefault("added", 0)
    result.setdefault("remaining_candidates", 0)
    result.setdefault("samples", [])
    result.setdefault("warnings", [])
    result.setdefault("missing_hint", "")
    if result.get("missing_hint"):
        result["hint"] = (
            "如字段缺失较多：先 analyze_coverage 自检，再 inspect_site 看结构，"
            "必要时 save_site_profile 写配置后再次 crawl_site 补抓。"
        )
    else:
        result["hint"] = (
            "可继续分析覆盖或保存输出。若仍有剩余候选且未达目标，可再次调用 crawl_site 补抓。"
        )
    if result.get("added", 0) and int(result.get("total_articles", 0)) >= target > 0:
        result["hint"] = "已达到目标篇数，请调用 analyze_coverage 自检后 save_outputs 保存。"
    return _json_safe(result)


def _tool_save_site_profile(arguments: dict[str, Any], session: Any) -> dict[str, Any]:
    profile = arguments.get("profile")
    if profile is None and ("domain" in arguments or "article" in arguments or "list" in arguments):
        # 容忍模型把 profile 字段直接平铺在参数顶层。
        profile = {key: value for key, value in arguments.items() if key != "profile"}
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except json.JSONDecodeError as error:
            return {"error": f"profile 不是合法 JSON：{error}"}
    if not isinstance(profile, dict):
        return {"error": "profile 必须是 JSON 对象（包含 domain/list/article 等字段）。"}
    saved = session.apply_profile(profile)
    return _json_safe(saved if isinstance(saved, dict) else {"saved": True})


def _tool_analyze_coverage(arguments: dict[str, Any], session: Any) -> dict[str, Any]:
    summary = session.analyze()
    if not isinstance(summary, dict):
        return {"error": "覆盖分析没有返回有效结果。"}
    return _json_safe(summary)


def _tool_save_outputs(arguments: dict[str, Any], session: Any) -> dict[str, Any]:
    report = arguments.get("report_markdown")
    if report is None:
        report = arguments.get("report")
    report_text = str(report).strip() if report else ""
    paths = session.save_outputs(report_text or None)
    if not isinstance(paths, dict) or not paths.get("dir"):
        return {"saved": False, "error": "输出文件没有成功写入，请检查磁盘权限后重试。"}
    files = {
        key: Path(value).name
        for key, value in paths.items()
        if key != "dir" and value
    }
    result = getattr(session, "result", None)
    article_count = len(getattr(result, "articles", [])) if result is not None else 0
    return _json_safe({
        "saved": True,
        "output_dir": paths.get("dir", ""),
        "files": files,
        "article_count": article_count,
        "report_chars": len(report_text),
        "hint": "输出已保存。若后续继续补抓，可再次调用 save_outputs 覆盖同一输出目录。",
    })


_HANDLERS = {
    "inspect_site": _tool_inspect_site,
    "crawl_site": _tool_crawl_site,
    "save_site_profile": _tool_save_site_profile,
    "analyze_coverage": _tool_analyze_coverage,
    "save_outputs": _tool_save_outputs,
}


# ---------------------------------------------------------------------------
# 对外分发入口
# ---------------------------------------------------------------------------

def execute_tool(name: str, arguments: Any, session: Any) -> dict[str, Any]:
    """执行一个 Function Calling 工具，返回 JSON 可序列化的结果。

    任何异常都转成 {"error": "工具执行失败：..."}，由 Agent Loop 回传给模型，
    保证单次工具失败不会中断整个任务。
    """
    tool_name = str(name or "").strip()
    args, error = _parse_arguments(arguments)
    if error:
        return {"error": error}
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        return {
            "error": f"未知工具：{tool_name or '（空）'}；可用工具：{', '.join(TOOL_NAMES)}"
        }
    try:
        result = handler(args, session)
    except Exception as error:  # noqa: BLE001 - 工具失败必须回传模型而不是崩溃
        return {"error": f"工具执行失败：{type(error).__name__}: {error}"}
    if not isinstance(result, dict):
        result = {"result": result}
    return _json_safe(result)
