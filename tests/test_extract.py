# -*- coding: utf-8 -*-
"""news_agent.extract 单元测试：真实站点 fixtures + 配置选择器/通用算法/多语言/容错。

fixtures 来自 tests/fixtures/（BBC/DW/NHK/新浪/联合早报，真实页面快照）。
为让测试在“未安装 trafilatura”的环境同样通过，默认把 extract._TRAFILATURA_STATE
的 extract 槽位设为 None（等价于隐藏可选依赖）；trafilatura 相关行为单独测试。
"""

from __future__ import annotations

import re
import unittest

from news_agent import extract
from news_agent.errors import ExtractError
from news_agent.models import CrawlConfig, NewsItem, SiteProfile
from tests.fixtures.loader import fixture_names, load_fixture

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)$")
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")

# (fixture 名, 期望语言, 正文最小长度, 标题应包含的关键词)
ARTICLE_EXPECTATIONS = [
    ("bbc_article_1", "en", 5000, "spain"),
    ("bbc_article_2", "en", 6000, "flood"),
    ("dw_article_1", "en", 7000, "israel"),
    ("dw_article_2", "en", 2000, "spain"),
    ("nhk_article_1", "ja", 60, "スペイン"),
    ("sina_article_1", "zh", 400, "红军"),
    ("sina_article_2", "zh", 1200, "反腐败"),
    ("zaobao_article_1", "zh", 1000, "中期选举"),
    ("sina_article_gbk", "zh", 400, "红军"),
]

PROFILE_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Title Tag Headline | Example Site</title>
<meta property="og:title" content="OG Title Text">
<meta name="description" content="Meta description used by summary field when long enough.">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"NewsArticle","headline":"JSON-LD Title",
 "datePublished":"2026-10-28T09:00:00+00:00","author":{"@type":"Person","name":"JSON Author"},
 "publisher":{"@type":"Organization","name":"JSON Publisher"},
 "articleBody":"JSON-LD article body sentence. JSON-LD second sentence."}
</script></head>
<body><article class="story">
<h1 id="headline">Configured H1 Headline</h1>
<div class="byline author">By Configured Author</div>
<time datetime="2026-10-29T08:30:00+00:00">29 Oct 2026</time>
<span data-source="Configured Source"></span>
<div class="section">World</div>
<p class="body-p">Configured paragraph one with enough text to be treated as real content by the extractor.</p>
<p class="body-p">Configured paragraph two continues the article body with further detail and context.</p>
<p class="body-p">Configured paragraph three closes the article body with a final remark.</p>
</article></body></html>"""

GENERIC_HTML = """<html lang="ja"><head><meta charset="utf-8"><title>サイト名</title></head>
<body><div class="wrapper"><article><h1>汎用アルゴリズムの見出し</h1>
<p>これは一つ目の段落です。十分な長さの本文がここにあります。記事の内容を詳しく説明しています。</p>
<p>これは二つ目の段落です。さらに詳しい説明が続きます。関係者によると状況は変化しているとのことです。</p>
<p>これは三つ目の段落です。記事の締めくくりとして今後の見通しについて述べています。</p></article>
<div class="related">関連記事へのリンク集です。こちらも長いテキストですが本文ではありません。</div></div></body></html>"""


def config_for(url: str, **kwargs: object) -> CrawlConfig:
    return CrawlConfig(entry_url=url, max_body_chars=20000, **kwargs)  # type: ignore[arg-type]


class TrafilaturaHiddenMixin:
    """每个测试前隐藏 trafilatura，保证测试结果与可选依赖安装状态无关。"""

    def setUp(self) -> None:
        self._saved_state = dict(extract._TRAFILATURA_STATE)
        extract._TRAFILATURA_STATE.clear()
        extract._TRAFILATURA_STATE.update({"checked": True, "extract": None, "failed": 0})

    def tearDown(self) -> None:
        extract._TRAFILATURA_STATE.clear()
        extract._TRAFILATURA_STATE.update(self._saved_state)


class FixtureArticleTest(TrafilaturaHiddenMixin, unittest.TestCase):
    def test_article_fixtures_fields(self) -> None:
        for name, language, min_body, keyword in ARTICLE_EXPECTATIONS:
            with self.subTest(fixture=name):
                fixture = load_fixture(name)
                article = extract.extract_from_html(
                    fixture.text, fixture.url, config_for(fixture.url)
                )
                # 标题 / 正文
                self.assertTrue(article.title, "标题为空")
                self.assertGreaterEqual(len(article.title), 8)
                self.assertIn(keyword, article.title.lower())
                self.assertGreaterEqual(len(article.body), min_body)
                self.assertGreater(article.word_count, 0)
                # 时间 / 来源 / 语言
                self.assertRegex(article.published_at, ISO_RE)
                self.assertTrue(article.source)
                self.assertEqual(article.language, language)
                # 元数据
                self.assertTrue(article.canonical_url)
                self.assertTrue(article.site_domain)
                self.assertIsInstance(article.images, list)
                self.assertGreaterEqual(article.confidence, 0.35)
                self.assertIn("title=", article.extract_method)
                self.assertIn("body=", article.extract_method)
                # 编码：不得出现替换字符/乱码
                self.assertNotIn("\ufffd", article.title)
                self.assertNotIn("\ufffd", article.body)
                if language in ("zh", "ja"):
                    self.assertGreater(len(CJK_RE.findall(article.title)), 3)
                    self.assertGreater(len(CJK_RE.findall(article.body)), 20)

    def test_chinese_article_text_is_readable(self) -> None:
        fixture = load_fixture("sina_article_1")
        article = extract.extract_from_html(fixture.text, fixture.url, config_for(fixture.url))
        self.assertIn("红军", article.title)
        self.assertIn("茅台", article.body)
        self.assertNotIn("\x00", article.body)

    def test_nhk_japanese_fields(self) -> None:
        fixture = load_fixture("nhk_article_1")
        article = extract.extract_from_html(fixture.text, fixture.url, config_for(fixture.url))
        self.assertEqual(article.language, "ja")
        self.assertIn("スペイン", article.title)
        self.assertRegex(article.published_at, r"\+09:00$")


class ProfileSelectorTest(TrafilaturaHiddenMixin, unittest.TestCase):
    def test_profile_selectors_take_priority(self) -> None:
        profile = SiteProfile.from_dict({
            "domain": "example.com",
            "language": "en",
            "article": {
                "title": "#headline",
                "body": "article.story p.body-p",
                "date": "time::attr(datetime)",
                "author": ".byline",
                "source": "[data-source]::attr(data-source)",
                "summary": "meta[name=description]::attr(content)",
                "section": ".section",
            },
        })
        article = extract.extract_from_html(
            PROFILE_HTML,
            "https://example.com/news/2026/10/29/story",
            config_for("https://example.com/"),
            profile=profile,
        )
        self.assertEqual(article.title, "Configured H1 Headline")
        for paragraph in ("paragraph one", "paragraph two", "paragraph three"):
            self.assertIn(paragraph, article.body)
        self.assertEqual(article.published_at, "2026-10-29T08:30:00+00:00")
        self.assertEqual(article.author, "Configured Author")
        self.assertEqual(article.source, "Configured Source")
        self.assertEqual(article.section, "World")
        self.assertIn("Meta description", article.summary)
        self.assertIn("title=profile", article.extract_method)
        self.assertIn("body=profile", article.extract_method)
        self.assertIn("date=profile", article.extract_method)

    def test_profile_exclude_removes_noise(self) -> None:
        html = """<html lang="en"><head><meta charset="utf-8"></head><body><article>
<h1>Exclude Test Headline</h1>
<p class="body-p">Kept paragraph one with enough content to be extracted as article body.</p>
<p class="body-p">Kept paragraph two with enough content to be extracted as article body.</p>
<p class="body-p">Kept paragraph three with enough content to be extracted as article body.</p>
<div class="ad">Advertise here with a long noisy sentence that must be removed from body.</div>
</article></body></html>"""
        profile = SiteProfile.from_dict({
            "domain": "example.com",
            "article": {"body": "article p", "exclude": [".ad"]},
        })
        article = extract.extract_from_html(
            html, "https://example.com/a", config_for("https://example.com/"), profile=profile
        )
        self.assertIn("Kept paragraph one", article.body)
        self.assertNotIn("Advertise here", article.body)


class GenericAlgorithmTest(TrafilaturaHiddenMixin, unittest.TestCase):
    def test_generic_body_keeps_multiple_paragraphs(self) -> None:
        article = extract.extract_from_html(
            GENERIC_HTML, "https://example.jp/news/1", config_for("https://example.jp/")
        )
        self.assertEqual(article.title, "汎用アルゴリズムの見出し")
        self.assertIn("一つ目の段落", article.body)
        self.assertIn("二つ目の段落", article.body)
        self.assertIn("三つ目の段落", article.body)
        self.assertNotIn("関連記事へのリンク集", article.body)
        self.assertEqual(article.language, "ja")
        self.assertIn("title=h1", article.extract_method)
        self.assertIn("body=generic", article.extract_method)

    def test_structured_data_fallback(self) -> None:
        article = extract.extract_from_html(
            PROFILE_HTML,
            "https://example.com/news/2026/10/29/story",
            config_for("https://example.com/"),
        )
        self.assertEqual(article.title, "JSON-LD Title")
        self.assertEqual(article.author, "JSON Author")
        self.assertEqual(article.source, "JSON Publisher")
        self.assertEqual(article.published_at, "2026-10-28T09:00:00+00:00")
        self.assertIn("Configured paragraph one", article.body)

    def test_date_from_url_when_page_has_no_date(self) -> None:
        html = """<html lang="en"><head><meta charset="utf-8"></head><body><article>
<h1>URL Date Article Headline</h1>
<p>This is a sufficiently long paragraph that describes the article content in detail for extraction.</p>
<p>Another paragraph continues the story with more context and quotes from the people involved.</p>
</article></body></html>"""
        article = extract.extract_from_html(
            html, "https://example.com/news/2026/10/29/slug", config_for("https://example.com/")
        )
        self.assertEqual(article.published_at, "2026-10-29T00:00:00+00:00")
        self.assertEqual(article.published_raw, "2026-10-29")

    def test_item_title_and_list_page_fallback(self) -> None:
        html = """<html><body><article>
<p>This is the article body without any title tags at all, long enough for extraction to work.</p>
<p>Second paragraph of the untitled article gives more details about what happened today.</p>
</article></body></html>"""
        item = NewsItem(
            url="https://example.com/news/1",
            title="Item Provided Title",
            list_url="https://example.com/news/",
        )
        article = extract.extract_from_html(
            html,
            "https://example.com/news/1",
            config_for("https://example.com/"),
            item=item,
        )
        self.assertEqual(article.title, "Item Provided Title")
        self.assertEqual(article.list_page, "https://example.com/news/")


class TrafilaturaFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_state = dict(extract._TRAFILATURA_STATE)

    def tearDown(self) -> None:
        extract._TRAFILATURA_STATE.clear()
        extract._TRAFILATURA_STATE.update(self._saved_state)

    def _set_state(self, extract_fn: object) -> None:
        extract._TRAFILATURA_STATE.clear()
        extract._TRAFILATURA_STATE.update({"checked": True, "extract": extract_fn, "failed": 0})

    def test_missing_trafilatura_uses_generic_algorithm(self) -> None:
        self._set_state(None)
        article = extract.extract_from_html(
            GENERIC_HTML, "https://example.jp/news/1", config_for("https://example.jp/")
        )
        self.assertIn("body=generic", article.extract_method)
        self.assertIn("二つ目の段落", article.body)

    def test_broken_trafilatura_falls_back(self) -> None:
        calls: list[str] = []

        def broken(*args: object, **kwargs: object) -> str:
            calls.append("called")
            raise RuntimeError("trafilatura boom")

        self._set_state(broken)
        article = extract.extract_from_html(
            GENERIC_HTML, "https://example.jp/news/1", config_for("https://example.jp/")
        )
        self.assertIn("body=generic", article.extract_method)
        self.assertIn("二つ目の段落", article.body)
        self.assertGreaterEqual(extract._TRAFILATURA_STATE["failed"], 1)

    def test_working_trafilatura_body_preferred(self) -> None:
        self._set_state(lambda html, url=None, include_comments=False, include_tables=False: "X" * 200)
        article = extract.extract_from_html(
            GENERIC_HTML, "https://example.jp/news/1", config_for("https://example.jp/")
        )
        self.assertIn("body=trafilatura", article.extract_method)
        self.assertEqual(article.body, "X" * 200)


class ExtractErrorTest(TrafilaturaHiddenMixin, unittest.TestCase):
    def test_empty_html_raises(self) -> None:
        for html in ("", "   "):
            with self.subTest(html=html), self.assertRaises(ExtractError):
                extract.extract_from_html(html, "https://example.com/a", config_for("https://example.com/"))

    def test_bytes_input_accepted(self) -> None:
        article = extract.extract_from_html(
            GENERIC_HTML.encode("utf-8"), "https://example.jp/news/1", config_for("https://example.jp/")
        )
        self.assertTrue(article.title)

    def test_title_only_page_returns_with_empty_body(self) -> None:
        html = "<html><head><title>Some Long Title Here</title></head><body><h1>Headline Only</h1></body></html>"
        article = extract.extract_from_html(
            html, "https://example.com/a", config_for("https://example.com/")
        )
        self.assertTrue(article.title)
        self.assertIsInstance(article.body, str)


if __name__ == "__main__":
    unittest.main()
