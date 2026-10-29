# -*- coding: utf-8 -*-
"""news_agent.output 单元测试：JSONL / CSV / run_meta / report / SVG 流程图字段与编码。"""

from __future__ import annotations

import csv
import json
import re
import tempfile
import unittest
from pathlib import Path

from news_agent.errors import NewsAgentError
from news_agent.models import (
    ARTICLE_FIELDS,
    SCHEMA_VERSION,
    Article,
    CrawlConfig,
    CrawlResult,
    CrawlStats,
)
from news_agent.output import CSV_COLUMNS, build_template_report, write_outputs

OUTPUT_KEYS = {"dir", "jsonl", "csv", "meta", "report", "diagram_svg", "diagram_png"}


def make_result() -> CrawlResult:
    result = CrawlResult(
        entry_url="https://news.example.com/",
        domain="news.example.com",
        profile_used="sites/news.example.com.json",
        discovery_method="feed",
        feeds=["https://news.example.com/rss.xml"],
    )
    result.add_article(
        Article(
            url="https://news.example.com/a",
            canonical_url="https://news.example.com/a",
            title="English headline about the world",
            summary="English summary",
            body="English body paragraph one.\nEnglish body paragraph two.",
            published_at="2026-10-29T10:00:00+00:00",
            author="Jane Doe",
            source="Example News",
            section="World",
            language="en",
            confidence=0.95,
            word_count=8,
            list_page="https://news.example.com/",
        )
    )
    result.add_article(
        Article(
            url="https://news.example.com/b",
            title="中文新闻标题：今日要闻",
            body="这是中文正文第一段。\n这是中文正文第二段。",
            published_at="2026-10-28T09:30:00+08:00",
            source="示例新闻",
            language="zh",
            word_count=20,
        )
    )
    result.stats.candidates = 10
    result.stats.list_pages = 2
    result.stats.extracted = 2
    result.stats.pages_fetched = 4
    result.stats.failed = 1
    result.stats.duplicates = 3
    result.stats.note_method("discovery", "feed")
    result.stats.note_method("extract", "jsonld")
    result.stats.add_error("https://news.example.com/broken", "fetch", "HTTP 503")
    result.stats.add_warning("单篇抽取失败")
    result.warnings.append("列表页翻页达到上限")
    return result


class OutputBaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out_root = self.root / "out"
        self.settings = {"output": {"dir": str(self.out_root)}}
        self.config = CrawlConfig(entry_url="https://news.example.com/", out_dir=str(self.out_root))
        self.result = make_result()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, **kwargs: object) -> dict[str, str]:
        return write_outputs(self.result, self.config, settings=self.settings, **kwargs)  # type: ignore[arg-type]


class WriteOutputsTest(OutputBaseTest):
    def test_returns_fixed_file_keys_and_creates_files(self) -> None:
        files = self.write()
        self.assertEqual(set(files), OUTPUT_KEYS)
        out_dir = Path(files["dir"])
        self.assertTrue(out_dir.is_dir())
        self.assertEqual(out_dir.parent, self.out_root)
        for key in ("jsonl", "csv", "meta", "report", "diagram_svg"):
            self.assertTrue(Path(files[key]).is_file(), key)
        self.assertEqual(files["diagram_png"], "")

    def test_jsonl_rows_and_schema(self) -> None:
        files = self.write()
        text = Path(files["jsonl"]).read_text(encoding="utf-8")
        self.assertIn("中文新闻标题", text)  # ensure_ascii=False
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        self.assertEqual(len(rows), 2)
        for row, article in zip(rows, self.result.articles):
            self.assertEqual(list(row), list(ARTICLE_FIELDS))
            self.assertEqual(row["id"], article.id)
        self.assertEqual(rows[1]["language"], "zh")
        self.assertEqual(rows[0]["title"], "English headline about the world")

    def test_csv_has_bom_header_and_rows(self) -> None:
        files = self.write()
        raw = Path(files["csv"]).read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        with Path(files["csv"]).open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], list(CSV_COLUMNS))
        self.assertEqual(len(rows), 3)
        titles = [row[CSV_COLUMNS.index("title")] for row in rows[1:]]
        self.assertEqual(titles, ["English headline about the world", "中文新闻标题：今日要闻"])

    def test_run_meta_content(self) -> None:
        files = self.write()
        meta = json.loads(Path(files["meta"]).read_text(encoding="utf-8"))
        self.assertEqual(meta["schema_version"], SCHEMA_VERSION)
        self.assertEqual(meta["entry_url"], "https://news.example.com/")
        self.assertEqual(meta["domain"], "news.example.com")
        self.assertEqual(meta["article_count"], 2)
        self.assertEqual(meta["discovery_method"], "feed")
        self.assertEqual(meta["config"]["out_dir"], str(self.out_root))
        self.assertEqual(meta["stats"]["candidates"], 10)
        self.assertEqual(len(meta["errors"]), 1)
        self.assertTrue(meta["warnings"])
        self.assertEqual(
            set(meta["output_files"]),
            {"jsonl", "csv", "meta", "report", "diagram_svg"},
        )
        self.assertEqual(meta["output_files"]["jsonl"], "articles.jsonl")

    def test_report_contains_statistics_and_links(self) -> None:
        files = self.write()
        report = Path(files["report"]).read_text(encoding="utf-8")
        self.assertIn("# 新闻抓取报告", report)
        self.assertIn("https://news.example.com/", report)
        self.assertIn("字段完整率", report)
        self.assertIn("English headline about the world", report)
        self.assertIn("https://news.example.com/a", report)
        self.assertIn("## 输出文件", report)
        self.assertIn("articles.jsonl", report)
        self.assertIn("列表页翻页达到上限", report)

    def test_svg_diagram_rendered(self) -> None:
        files = self.write()
        svg = Path(files["diagram_svg"]).read_text(encoding="utf-8")
        self.assertIn("<svg", svg)
        self.assertIn("</svg>", svg)
        self.assertIn("AGENT RUN DIAGRAM", svg)
        self.assertNotIn("__HEIGHT__", svg)
        self.assertIn("通用新闻抓取流水线", svg)

    def test_custom_report_markdown_used(self) -> None:
        files = self.write(report_markdown="# 自定义报告\n\n这是模型生成的报告。")
        report = Path(files["report"]).read_text(encoding="utf-8")
        self.assertTrue(report.startswith("# 自定义报告"))
        self.assertIn("## 输出文件", report)

    def test_renderer_png(self) -> None:
        files = self.write(renderer=lambda svg, width, height: b"FAKE-PNG")
        self.assertTrue(files["diagram_png"])
        self.assertEqual(Path(files["diagram_png"]).read_bytes(), b"FAKE-PNG")

    def test_renderer_failure_keeps_svg(self) -> None:
        def broken(svg: str, width: int, height: int) -> bytes:
            raise RuntimeError("render boom")

        files = self.write(renderer=broken)
        self.assertEqual(files["diagram_png"], "")
        self.assertTrue(Path(files["diagram_svg"]).is_file())

    def test_output_dir_can_be_reused(self) -> None:
        first = self.write()
        second = write_outputs(
            self.result, self.config, settings=self.settings, output_dir=first["dir"]
        )
        self.assertEqual(first["dir"], second["dir"])
        self.assertEqual(Path(second["jsonl"]).parent, Path(second["dir"]))

    def test_empty_result_still_writes_dataset(self) -> None:
        empty = CrawlResult(entry_url="https://news.example.com/", domain="news.example.com")
        files = write_outputs(empty, self.config, settings=self.settings)
        self.assertEqual(Path(files["jsonl"]).read_text(encoding="utf-8"), "")
        with Path(files["csv"]).open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows, [list(CSV_COLUMNS)])
        report = Path(files["report"]).read_text(encoding="utf-8")
        self.assertIn("本次没有抽取到文章", report)

    def test_invalid_argument_types_raise(self) -> None:
        with self.assertRaises(NewsAgentError):
            write_outputs("not-a-result", self.config)  # type: ignore[arg-type]
        with self.assertRaises(NewsAgentError):
            write_outputs(self.result, "not-a-config")  # type: ignore[arg-type]


class TemplateReportTest(unittest.TestCase):
    def test_report_covers_completeness_language_and_dates(self) -> None:
        result = make_result()
        report = build_template_report(result)
        self.assertIn("新闻抓取报告", report)
        self.assertIn("字段完整率", report)
        self.assertIn("语言分布", report)
        self.assertIn("发布时间范围", report)
        self.assertIn("2026-10-28", report)
        self.assertIn("2026-10-29", report)
        self.assertIn("文章列表", report)
        self.assertIn("采集问题", report)
        self.assertIn("HTTP 503", report)
        self.assertRegex(report, r"\| 标题 \| 2/2 \|")

    def test_report_without_articles(self) -> None:
        result = CrawlResult(entry_url="https://x.com/", domain="x.com")
        report = build_template_report(result)
        self.assertIn("本次没有抽取到文章", report)
        self.assertIn("0 篇", report)


if __name__ == "__main__":
    unittest.main()
