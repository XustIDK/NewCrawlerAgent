"""命令行入口：python -m news_agent --url <新闻网站> [选项]。

示例：
    python -m news_agent --url https://www.bbc.com/news --limit 3 --no-llm
    python -m news_agent --url https://www3.nhk.or.jp/news/ --limit 2 --visible
    python -m news_agent --url https://feeds.bbci.co.uk/news/rss.xml --no-llm

逐个站点执行：有 LLM 时走 Agent Loop，--no-llm 时走确定性流水线。
至少一篇文章抓取成功返回 0，否则返回 1；网址非法返回 2。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable

from . import __version__, config as config_module
from .agent import run_agent, run_deterministic
from .models import Article, CrawlConfig, CrawlResult
from .urls import normalize_entry_url

FIELD_LABELS = {
    "title": "标题",
    "body": "正文",
    "published_at": "发布时间",
    "author": "作者",
    "source": "来源",
    "language": "语言",
}


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="news_agent",
        description="通用新闻抓取 Agent：输入任意新闻网站网址，自动发现文章并输出结构化数据集。",
        epilog="示例：python -m news_agent --url https://www.bbc.com/news --limit 3 --no-llm",
    )
    parser.add_argument(
        "--url",
        action="append",
        required=True,
        metavar="URL",
        help="新闻网站网址（列表页或 RSS 均可），可多次指定，逐个执行。",
    )
    parser.add_argument("--limit", type=int, default=None, metavar="N", help="每个站点最多抓取的文章数。")
    parser.add_argument("--pages", type=int, default=None, metavar="N", help="列表页最多翻页数。")
    parser.add_argument("--lang", default=None, metavar="LANG", help="语言提示，例如 zh-CN / en / ja。")
    parser.add_argument("--no-llm", action="store_true", help="不使用 LLM，走确定性流水线（无需 API Key）。")
    parser.add_argument("--visible", action="store_true", help="显示浏览器窗口（headless=False，可人工处理验证码）。")
    parser.add_argument(
        "--browser",
        choices=("auto", "always", "never"),
        default=None,
        help="浏览器模式：auto 自动回退（默认）、always 强制、never 禁用。",
    )
    parser.add_argument("--no-robots", action="store_true", help="忽略 robots.txt（请自行确认目标站点合规要求）。")
    parser.add_argument("--out", default=None, metavar="DIR", help="自定义输出根目录（默认 data/）。")
    parser.add_argument("--json", action="store_true", help="在 stdout 输出机器可读的 JSON 结果摘要。")
    parser.add_argument("--quiet", action="store_true", help="不打印抓取进度。")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


# ---------------------------------------------------------------------------
# 摘要与打印
# ---------------------------------------------------------------------------

def _completeness(articles: list[Article]) -> dict[str, float]:
    total = len(articles)
    result: dict[str, float] = {}
    for field in FIELD_LABELS:
        count = sum(1 for article in articles if getattr(article, field, ""))
        result[field] = round(count / total, 4) if total else 0.0
    return result


def _build_summary(
    entry: str,
    result: CrawlResult | None,
    final_answer: str = "",
    error: str = "",
) -> dict[str, Any]:
    if result is None:
        return {
            "url": entry,
            "ok": False,
            "article_count": 0,
            "field_completeness": {},
            "output_dir": "",
            "files": {},
            "final_answer": final_answer,
            "errors": [error] if error else [],
            "warnings": [],
        }
    articles = list(result.articles)
    return {
        "url": entry,
        "ok": bool(articles),
        "article_count": len(articles),
        "field_completeness": _completeness(articles),
        "output_dir": result.output_files.get("dir", ""),
        "files": {key: value for key, value in result.output_files.items() if key != "dir"},
        "final_answer": final_answer,
        "errors": [
            {
                "stage": error.get("stage", ""),
                "url": error.get("url", ""),
                "error": error.get("error", ""),
            }
            for error in result.stats.errors[:10]
        ],
        "warnings": list(result.stats.warnings)[:10],
    }


def _make_progress(args: argparse.Namespace) -> Callable[..., None]:
    def progress(message: str, phase: str | None = None) -> None:
        if args.quiet:
            return
        stream = sys.stderr if args.json else sys.stdout
        prefix = f"[{phase}] " if phase else ""
        print(f"{prefix}{message}", file=stream, flush=True)

    return progress


def _print_result(summary: dict[str, Any], final_answer: str) -> None:
    print(f"\n=== 抓取结果：{summary['url']} ===")
    count = int(summary.get("article_count", 0))
    print(f"文章数量：{count}")
    rates = summary.get("field_completeness") or {}
    if rates:
        rate_text = " · ".join(
            f"{FIELD_LABELS.get(field, field)} {float(rate) * 100:.0f}%"
            for field, rate in rates.items()
        )
        print(f"字段完整率：{rate_text}")
    output_dir = summary.get("output_dir") or ""
    print(f"输出目录：{output_dir or '（未生成）'}")
    files = summary.get("files") or {}
    if files:
        print("输出文件：" + "、".join(str(value) for value in files.values()))
    if final_answer:
        print("\nAgent 最终答复：")
        print(final_answer)
    errors = summary.get("errors") or []
    if errors:
        print(f"\n采集错误（共 {len(errors)} 条，前 3 条）：")
        for error in errors[:3]:
            stage = error.get("stage", "") if isinstance(error, dict) else ""
            message = error.get("error", "") if isinstance(error, dict) else str(error)
            print(f"  ! [{stage}] {message}")
    warnings = summary.get("warnings") or []
    if not count:
        if warnings:
            print("\n提示：")
            for warning in warnings[:3]:
                print(f"  - {warning}")
        print("\n本次没有抓到文章：请检查网址是否为新闻列表页、网络是否可访问，或改用 --visible/--browser always 重试。")
    else:
        print(f"\n数据与报告已保存，可直接打开 {output_dir}")


def _build_overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {"use_llm": not args.no_llm}
    if args.limit is not None:
        overrides["max_articles"] = max(1, int(args.limit))
    if args.pages is not None:
        overrides["max_list_pages"] = max(1, int(args.pages))
    if args.lang:
        overrides["language_hint"] = args.lang
    if args.visible:
        overrides["headless"] = False
    if args.browser:
        overrides["browser_mode"] = args.browser
    if args.no_robots:
        overrides["respect_robots"] = False
    if args.out:
        overrides["out_dir"] = args.out
    return overrides


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _harden_console() -> None:
    """重定向/非 UTF-8 控制台下容忍无法编码的字符（多语言新闻标题），避免打印时崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _harden_console()

    config_module.load_local_env()
    settings = config_module.load_settings()

    # 1) 先统一校验 URL：非法网址直接返回 2，避免半途失败。
    entries: list[str] = []
    invalid: list[tuple[str, str]] = []
    for raw in args.url or []:
        try:
            entries.append(normalize_entry_url(raw))
        except (ValueError, TypeError) as error:
            invalid.append((str(raw), str(error)))
    if invalid:
        for raw, message in invalid:
            print(f"[错误] 网址无效：{raw} —— {message}", file=sys.stderr)
        print(
            "提示：网址需为 http/https 地址，例如 https://www.bbc.com/news",
            file=sys.stderr,
        )
        return 2

    progress = _make_progress(args)
    summaries: list[dict[str, Any]] = []
    total_articles = 0
    for entry in entries:
        overrides = _build_overrides(args)
        try:
            crawl_config = config_module.build_config(entry, settings, **overrides)
        except (ValueError, TypeError) as error:
            print(f"[错误] 无法构造抓取配置：{entry} —— {error}", file=sys.stderr)
            summaries.append(_build_summary(entry, None, error=f"{type(error).__name__}: {error}"))
            continue

        if not args.quiet and not args.json:
            print(f"\n=== 新闻抓取：{entry} ===")
            print(
                f"目标 {crawl_config.max_articles} 篇 · 翻页上限 {crawl_config.max_list_pages} 页 · "
                f"浏览器 {crawl_config.browser_mode} · LLM {'否（确定性流水线）' if args.no_llm else '是'}"
            )

        final_answer = ""
        try:
            if args.no_llm:
                result = run_deterministic(
                    crawl_config,
                    settings=settings,
                    on_event=progress,
                )
            else:
                final_answer, result = run_agent(
                    crawl_config,
                    settings=settings,
                    on_event=progress,
                )
        except Exception as error:  # noqa: BLE001 - run_* 已兜底，这里防止意外
            message = f"{type(error).__name__}: {error}"
            print(f"[错误] {entry} 抓取失败：{message}", file=sys.stderr)
            summaries.append(_build_summary(entry, None, error=message))
            continue

        summary = _build_summary(entry, result, final_answer=final_answer)
        summaries.append(summary)
        total_articles += int(summary["article_count"])
        if not args.json:
            _print_result(summary, final_answer)

    if args.json:
        # --json 面向机器读取：纯 ASCII 转义，避免 Windows 重定向/子进程按 cp936 解码时乱码。
        print(json.dumps({"version": __version__, "results": summaries}, ensure_ascii=True, indent=2))

    return 0 if total_articles > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
