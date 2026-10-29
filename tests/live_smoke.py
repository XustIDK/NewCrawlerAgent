# -*- coding: utf-8 -*-
"""联网冒烟脚本（可选，手动执行；不纳入 unittest 自动发现）。

对真实新闻站点跑确定性流水线（不调用 LLM），打印字段完整率与文章样例，
用于人工验收网络可达性与抽取质量：

    .venv\\Scripts\\python.exe tests\\live_smoke.py --limit 2
    .venv\\Scripts\\python.exe tests\\live_smoke.py --url https://www.bbc.com/news --limit 2

输出写入系统临时目录，不会污染项目 data/。需要联网，请遵守目标站点条款。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

# 允许从项目根目录或任意目录直接执行：python tests/live_smoke.py
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from news_agent.agent import run_deterministic  # noqa: E402
from news_agent.config import build_config, load_settings  # noqa: E402

DEFAULT_SITES = (
    ("BBC", "https://www.bbc.com/news"),
    ("新浪新闻", "https://news.sina.com.cn/"),
)


def smoke_one(name: str, url: str, limit: int, browser_mode: str = "") -> bool:
    settings = load_settings()
    browser_text = f"，browser={browser_mode}" if browser_mode else ""
    print(f"\n=== {name}：{url}（limit={limit}，no-llm{browser_text}）===")
    overrides = {"browser_mode": browser_mode} if browser_mode else {}
    with tempfile.TemporaryDirectory() as tmp:
        config = build_config(
            url,
            settings,
            max_articles=limit,
            max_list_pages=1,
            use_llm=False,
            out_dir=tmp,
            **overrides,
        )
        result = run_deterministic(config, settings)
        articles = result.articles
        print(f"发现方式：{result.discovery_method or 'none'}；候选 {result.stats.candidates}；"
              f"抓取 {result.stats.pages_fetched} 页；成功 {len(articles)} 篇")
        if result.stats.errors:
            print(f"错误 {len(result.stats.errors)} 条，示例：{result.stats.errors[0]}")
        if result.stats.warnings:
            print(f"警告 {len(result.stats.warnings)} 条，示例：{result.stats.warnings[0]}")
        completeness = {}
        for field in ("title", "body", "published_at", "author", "source", "language"):
            count = sum(1 for article in articles if getattr(article, field, ""))
            rate = count / len(articles) if articles else 0.0
            completeness[field] = f"{rate * 100:.0f}%"
        print("字段完整率：" + "  ".join(f"{field} {rate}" for field, rate in completeness.items()))
        for index, article in enumerate(articles[:limit], start=1):
            print(f"  {index}. [{article.language or '?'}] {article.title[:60]}")
            print(f"     时间：{article.published_at or '未知'}；来源：{article.source or '未知'}；"
                  f"正文 {len(article.body)} 字；{article.url}")
        print(f"  → 临时输出：{Path(result.output_files.get('dir', '')).name or '无'}")
        return len(articles) > 0


def main() -> int:
    parser = argparse.ArgumentParser(description="新闻抓取 Agent 联网冒烟")
    parser.add_argument("--url", action="append", default=None, help="目标 URL，可重复；缺省跑 BBC + 新浪")
    parser.add_argument("--limit", type=int, default=2, help="每个站点抓取文章数，默认 2")
    parser.add_argument(
        "--browser",
        choices=("auto", "always", "never"),
        default="",
        help="覆盖浏览器模式（例如 Le Monde 列表 406 需 --browser always）",
    )
    args = parser.parse_args()
    targets = [(f"站点{i + 1}", url) for i, url in enumerate(args.url or [])]
    if not targets:
        targets = list(DEFAULT_SITES)
    ok = True
    for name, url in targets:
        ok = smoke_one(name, url, max(1, args.limit), args.browser) and ok
    print("\n冒烟结果：" + ("通过" if ok else "存在站点未抓到文章，请检查网络/站点结构"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
