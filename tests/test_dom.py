# -*- coding: utf-8 -*-
"""news_agent.dom 单元测试：CSS 子集/XPath/取值后缀/JSON-LD/meta/容器文本/坏选择器容错。"""

from __future__ import annotations

import unittest

from news_agent import dom

SAMPLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Sample &amp; Test — Example News</title>
  <meta property="og:title" content="OG title">
  <meta name="description" content="A sample description">
  <meta itemprop="datePublished" content="2026-10-29T10:30:00+00:00">
  <script type="application/ld+json">
  {"@context": "https://schema.org", "@type": "NewsArticle", "headline": "JSON-LD Headline",
   "datePublished": "2026-10-29T10:30:00+00:00", "articleBody": "JSON-LD body.",
   "author": {"@type": "Person", "name": "Jane Doe"},
   "publisher": {"@type": "Organization", "name": "Example News"}}
  </script>
  <script type="application/ld+json">
  {"@context": "https://schema.org", "@graph": [{"@type": "ItemList",
   "itemListElement": [{"@type": "ListItem", "position": 1, "url": "https://example.com/a"}]}]}
  </script>
  <script type="application/ld+json">{"@type": ["Article", "NewsArticle"], "headline": "Multi Type"}</script>
  <script type="application/ld+json">{invalid json,,}</script>
</head>
<body>
  <nav class="site-nav"><a href="/home">Home</a></nav>
  <main id="main" class="content">
    <article class="story" data-kind="news">
      <h1 class="headline">Real Headline</h1>
      <p class="lead byline" data-role="summary" data-label="hello world">First <b>paragraph</b> tail.</p>
      <p class="body">Second <a href="/related">link</a>.</p>
      <ul><li>Item one</li><li>Item two</li></ul>
      <div class="ad-banner">Advertisement text</div>
      <aside class="related">Related stories</aside>
      <time datetime="2026-10-29T10:30:00+00:00">29 Oct 2026</time>
      <img src="/img/a.jpg" alt="A">
      <span class="empty"></span>
    </article>
  </main>
  <footer>Footer text</footer>
</body>
</html>"""


def make_tree() -> "dom.etree._Element":
    return dom.parse_html(SAMPLE_HTML)


class ParseHtmlTest(unittest.TestCase):
    def test_parse_str_and_bytes(self) -> None:
        for content in (SAMPLE_HTML, SAMPLE_HTML.encode("utf-8")):
            with self.subTest(content_type=type(content).__name__):
                tree = dom.parse_html(content)
                self.assertEqual(dom.select_nodes(tree, "h1")[0].text, "Real Headline")

    def test_parses_chinese_text(self) -> None:
        tree = dom.parse_html("<html><body><p>中文新闻标题</p></body></html>")
        self.assertEqual(dom.first_value(tree, "p"), "中文新闻标题")

    def test_xml_declaration_is_stripped(self) -> None:
        tree = dom.parse_html('<?xml version="1.0" encoding="utf-8"?><html><body><p>x</p></body></html>')
        self.assertEqual(dom.first_value(tree, "p"), "x")

    def test_empty_content_raises_value_error(self) -> None:
        for content in (None, "", "   "):
            with self.subTest(content=content), self.assertRaises(ValueError):
                dom.parse_html(content)  # type: ignore[arg-type]


class CssSelectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = make_tree()

    def test_tag_class_id(self) -> None:
        self.assertEqual(len(dom.select_nodes(self.tree, "article")), 1)
        self.assertEqual(len(dom.select_nodes(self.tree, ".headline")), 1)
        self.assertEqual(len(dom.select_nodes(self.tree, "#main")), 1)
        self.assertEqual(len(dom.select_nodes(self.tree, ".lead.byline")), 1)
        self.assertEqual(len(dom.select_nodes(self.tree, "#main .story")), 1)

    def test_attribute_selectors(self) -> None:
        cases = [
            "p[data-role]",
            "p[data-role=summary]",
            "p[data-role='summary']",
            'p[data-role="summary"]',
            "p[data-role~=summary]",
            "p[data-role^=sum]",
            "p[data-role$=ary]",
            "p[data-role*=umm]",
            "time[datetime]",
            "img[src]",
            "article[data-kind]",
        ]
        for selector in cases:
            with self.subTest(selector=selector):
                self.assertEqual(len(dom.select_nodes(self.tree, selector)), 1)

    def test_descendant_and_child_combinators_with_optional_spaces(self) -> None:
        self.assertEqual(len(dom.select_nodes(self.tree, "article p")), 2)
        self.assertEqual(len(dom.select_nodes(self.tree, "article>p")), 2)
        self.assertEqual(len(dom.select_nodes(self.tree, "article > p")), 2)
        self.assertEqual(len(dom.select_nodes(self.tree, "article > h1")), 1)

    def test_sibling_combinators_with_optional_spaces(self) -> None:
        self.assertEqual(len(dom.select_nodes(self.tree, "h1 + p")), 1)
        self.assertEqual(len(dom.select_nodes(self.tree, "h1+p")), 1)
        self.assertEqual(len(dom.select_nodes(self.tree, "h1 ~ p")), 2)
        self.assertEqual(len(dom.select_nodes(self.tree, "h1~p")), 2)

    def test_pseudo_classes(self) -> None:
        self.assertEqual(dom.first_value(self.tree, "li:first-child"), "Item one")
        self.assertEqual(dom.first_value(self.tree, "li:last-child"), "Item two")
        self.assertEqual(dom.first_value(self.tree, "li:nth-child(2)"), "Item two")

    def test_comma_groups_return_document_order(self) -> None:
        values = dom.select_values(self.tree, "h1.headline, footer")
        self.assertEqual(values, ["Real Headline", "Footer text"])

    def test_attribute_suffix_forms(self) -> None:
        self.assertEqual(dom.select_values(self.tree, "nav a::attr(href)"), ["/home"])
        self.assertEqual(dom.select_values(self.tree, "article a::attr(href)"), ["/related"])
        self.assertEqual(len(dom.select_values(self.tree, "a[href]")), 2)
        self.assertEqual(
            dom.select_values(self.tree, "time::attr(datetime)"),
            ["2026-10-29T10:30:00+00:00"],
        )
        self.assertEqual(dom.select_values(self.tree, "p.lead::attr(class)"), ["lead byline"])


class XPathSelectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = make_tree()

    def test_xpath_prefix_and_bare_expression(self) -> None:
        self.assertEqual(dom.first_value(self.tree, "xpath://h1"), "Real Headline")
        self.assertEqual(dom.first_value(self.tree, "//article//time"), "29 Oct 2026")
        self.assertEqual(dom.first_value(self.tree, "xpath:.//p[@class='body']"), "Second link.")

    def test_selector_scope_is_subtree(self) -> None:
        article = dom.select_nodes(self.tree, "article")[0]
        self.assertEqual(len(dom.select_nodes(article, "p")), 2)
        self.assertEqual(len(dom.select_nodes(article, "xpath://p")), 2)
        self.assertEqual(len(dom.select_nodes(article, "h1")), 1)
        self.assertEqual(dom.select_nodes(article, "nav"), [])


class ValueSuffixTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = make_tree()

    def test_text_is_default(self) -> None:
        self.assertEqual(dom.first_value(self.tree, "h1"), "Real Headline")
        self.assertEqual(dom.select_values(self.tree, "h1::text"), ["Real Headline"])

    def test_text_and_html_suffix_variants(self) -> None:
        self.assertEqual(dom.select_values(self.tree, "p.lead::text()"), ["First paragraph tail."])
        self.assertEqual(dom.select_values(self.tree, "p.lead::text"), ["First paragraph tail."])
        html = dom.select_values(self.tree, "article::html")
        self.assertEqual(len(html), 1)
        self.assertIn("<h1", html[0])
        self.assertIn("Real Headline", html[0])
        self.assertEqual(dom.select_values(self.tree, "article::html()"), html)

    def test_drop_empty_behaviour(self) -> None:
        self.assertEqual(dom.select_values(self.tree, "span.empty"), [])
        self.assertEqual(dom.select_values(self.tree, "span.empty", drop_empty=False), [""])
        self.assertEqual(dom.first_value(self.tree, "span.empty"), "")


class BadSelectorTest(unittest.TestCase):
    def test_bad_selectors_return_empty_without_raising(self) -> None:
        tree = make_tree()
        bad_selectors = ["div[[[", "xpath://[", "p[", ":not(", "::attr(", "a[href=", "   ", ""]
        for selector in bad_selectors:
            with self.subTest(selector=selector):
                self.assertEqual(dom.select_nodes(tree, selector), [])
                self.assertEqual(dom.select_values(tree, selector), [])

    def test_none_root_is_safe(self) -> None:
        self.assertEqual(dom.select_nodes(None, "p"), [])
        self.assertEqual(dom.select_values(None, "p"), [])
        self.assertEqual(dom.first_value(None, "p"), "")
        self.assertEqual(dom.node_text(None), "")
        self.assertEqual(dom.container_text(None), "")


class TextHelperTest(unittest.TestCase):
    def test_node_text_and_clean_ws(self) -> None:
        tree = make_tree()
        node = dom.select_nodes(tree, "p.lead")[0]
        self.assertEqual(dom.node_text(node), "First paragraph tail.")
        self.assertEqual(dom.clean_ws(" a\u00a0 b\u200b c  d\n"), "a b c d")
        self.assertEqual(dom.clean_ws(""), "")

    def test_container_text_cleans_boilerplate_and_keeps_paragraphs(self) -> None:
        tree = make_tree()
        article = dom.select_nodes(tree, "article")[0]
        text = dom.container_text(article)
        self.assertIn("Real Headline", text)
        self.assertIn("First paragraph tail.", text)
        self.assertIn("Item one", text)
        self.assertNotIn("Advertisement", text)
        self.assertNotIn("Related stories", text)
        lines = text.splitlines()
        self.assertGreaterEqual(len(lines), 5)
        self.assertTrue(all(line.strip() for line in lines))
        inline = dom.container_text(article, sep=" | ")
        self.assertIn(" | ", inline)
        self.assertNotIn("\n", inline)

    def test_remove_boilerplate_with_extra_markers(self) -> None:
        tree = dom.parse_html(
            "<div id='root'><p>Keep</p><div class='custom-noise'>Drop</div>"
            "<div class='ad-slot'>Ad</div><script>var x = 1;</script><nav>Nav</nav></div>"
        )
        root = dom.select_nodes(tree, "#root")[0]
        dom.remove_boilerplate(root, extra_markers=("custom-noise",))
        text = dom.node_text(root)
        self.assertIn("Keep", text)
        self.assertNotIn("Drop", text)
        self.assertNotIn("Ad", text)
        self.assertNotIn("var x", text)
        self.assertNotIn("Nav", text)


class MetaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = make_tree()

    def test_meta_content_by_property_name_itemprop(self) -> None:
        self.assertEqual(dom.meta_content(self.tree, "og:title"), "OG title")
        self.assertEqual(dom.meta_content(self.tree, "OG:TITLE"), "OG title")
        self.assertEqual(dom.meta_content(self.tree, "description"), "A sample description")
        self.assertEqual(dom.meta_content(self.tree, "datePublished"), "2026-10-29T10:30:00+00:00")
        self.assertEqual(dom.meta_content(self.tree), "")
        self.assertEqual(dom.meta_content(None, "og:title"), "")

    def test_meta_content_by_prefix(self) -> None:
        self.assertEqual(dom.meta_content_by_prefix(self.tree, "og:"), "OG title")
        self.assertEqual(dom.meta_content_by_prefix(self.tree, "article:", "og:"), "OG title")
        self.assertEqual(dom.meta_content_by_prefix(self.tree, "missing:"), "")
        self.assertEqual(dom.meta_content_by_prefix(None, "og:"), "")


class JsonLdTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = make_tree()

    def test_json_ld_objects_flatten_graph_and_ignore_bad_json(self) -> None:
        objects = dom.json_ld_objects(self.tree)
        self.assertEqual(len(objects), 4)
        self.assertEqual(dom.json_ld_objects(None), [])

    def test_json_ld_by_type_and_first(self) -> None:
        news = dom.json_ld_by_type(self.tree, ["NewsArticle"])
        self.assertEqual(len(news), 2)
        self.assertEqual(dom.first_json_ld(self.tree, ["NewsArticle"])["headline"], "JSON-LD Headline")
        self.assertEqual(dom.first_json_ld(self.tree, ["itemlist"])["@type"], "ItemList")
        self.assertEqual(dom.first_json_ld(self.tree, ["MissingType"]), {})
        self.assertEqual(dom.json_ld_by_type(None, ["NewsArticle"]), [])

    def test_json_ld_text_shapes(self) -> None:
        self.assertEqual(dom.json_ld_text("  value "), "value")
        self.assertEqual(dom.json_ld_text({"name": "Name"}), "Name")
        self.assertEqual(dom.json_ld_text({"@value": "Value"}), "Value")
        self.assertEqual(dom.json_ld_text({"headline": "Headline"}), "Headline")
        self.assertEqual(dom.json_ld_text([None, {"name": "Nested"}]), "Nested")
        self.assertEqual(dom.json_ld_text(42), "42")
        self.assertEqual(dom.json_ld_text(None), "")
        self.assertEqual(dom.json_ld_text({"unknown": 1}), "")


if __name__ == "__main__":
    unittest.main()
