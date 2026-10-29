# -*- coding: utf-8 -*-
"""news_agent.discover 单元测试：RSS/JSON-LD/启发式/站点画像/翻页/feed 自动发现。

全部使用 tests/fixtures/ 的离线 HTML/RSS 与内联合成页面，通过 StubFetcher
模拟网络，不访问外网；翻页/feed 探测等“网络行为”也在 stub 路由内闭环。
"""

from __future__ import annotations

import json
import unittest

from news_agent import discover, dom
from news_agent.models import CrawlConfig, SiteProfile
from tests.fixtures.loader import load_fixture


class StubFetchResult:
    """最小 FetchResult 替身（与 fetch.FetchResult 的消费字段一致）。"""

    def __init__(self, url: str, text: str, status: int = 200) -> None:
        self.url = url
        self.final_url = url
        self.status = status
        self.text = text
        self.content = text.encode("utf-8", "replace")
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"
        self.method = "http"
        self.elapsed_ms = 1
        self.error = "" if status == 200 else f"HTTP {status}"

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and bool(self.text)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


class StubFetcher:
    """按完整 URL 路由返回固定内容；未命中返回 404。"""

    def __init__(self, routes: dict[str, str | tuple[int, str]]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: object) -> StubFetchResult:
        self.calls.append(url)
        key = url.split("?")[0]
        value = self.routes.get(key)
        if value is None:
            return StubFetchResult(url, "", 404)
        if isinstance(value, tuple):
            return StubFetchResult(url, value[1], value[0])
        return StubFetchResult(url, value)


def heuristic_page(prefix: str, date: str, count: int, title_prefix: str = "") -> str:
    items = "".join(
        f'<div class="item"><h2><a href="/article/{date}/story-{prefix}{index}">'
        f"{title_prefix}第{prefix}{index}条新闻标题，包含足够长度的文本</a></h2></div>"
        for index in range(1, count + 1)
    )
    return f"<html><head><title>新闻列表</title></head><body><div class=\"list\">{items}</div></body></html>"


JSONLD_PAGE = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"ItemList","itemListElement":[
 {"@type":"ListItem","position":1,"url":"https://example.com/news/2026/10/29/a"},
 {"@type":"ListItem","position":2,"url":"https://example.com/news/2026/10/29/b"},
 {"@type":"ListItem","position":3,"url":"https://example.com/news/2026/10/29/c"},
 {"@type":"ListItem","position":4,"url":"https://example.com/news/2026/10/29/d"}]}
</script></head><body><h1>List</h1></body></html>"""

PROFILE_PAGE = '<html><body><ul id="list">' + "".join(
    f'<li class="item"><a href="/news/2026/10/29/configured-{index}">'
    f"Configured title number {index} for profile test</a></li>"
    for index in range(1, 5)
) + "</ul></body></html>"

FEED_DISCOVERY_PAGE = (
    '<html><head><link rel="alternate" type="application/rss+xml" href="/rss.xml"></head>'
    '<body><h1>News site</h1></body></html>'
)

BBC_LIST_URL = "https://www.bbc.com/news"


class LooksLikeArticleUrlTest(unittest.TestCase):
    def test_article_urls_are_accepted(self) -> None:
        urls = [
            "https://www.bbc.com/news/articles/cm8xz7pkd0jjo",
            "https://www.dw.com/en/some-title/a-79788174",
            "https://www.lemonde.fr/en/international/article/2026/10/29/some-title_6766564_4.html",
            "https://news.sina.com.cn/c/2026-10-29/doc-iniwwpnx8888461.shtml",
            "https://www.zaobao.com.sg/news/world/story20261029-9910257",
            "https://example.com/2026/10/29/story",
            "https://example.com/news/12345",
            "https://example.com/a/b/c-d-e-f",
            "https://example.com/some-section/wordpress-post?p=12345",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertTrue(discover.looks_like_article_url(url))

    def test_channel_homepages_are_rejected(self) -> None:
        """根路径 / 频道首页（/news/、/world/、index.html）不作为文章候选。"""
        for url in (
            "https://www.bbc.com/",
            "https://www.bbc.com/news",
            "https://www.bbc.com/news/",
            "https://www.bbc.com/world/",
            "https://example.com/news/index.html",
        ):
            with self.subTest(url=url):
                self.assertFalse(discover.looks_like_article_url(url))

    def test_navigation_urls_are_rejected(self) -> None:
        urls = [
            "https://www.bbc.com/",
            "https://www.bbc.com/news",
            "https://www.bbc.com/news/world",
            "https://www.bbc.com/video/abc",
            "https://www.bbc.com/news/topics/xyz",
            "https://example.com/news/login",
            "https://example.com/images/photo.jpg",
            "https://example.com/robots.txt",
            "mailto:someone@example.com",
            "",
            None,
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertFalse(discover.looks_like_article_url(url))  # type: ignore[arg-type]

    def test_redirect_endpoints_are_rejected(self) -> None:
        """RSS 常见 redirect/jump 中转端点不是文章本身（会 302 到首页）。"""
        for url in (
            "http://go.rss.sina.com.cn/redirect.php?url=http://you.video.sina.com.cn/b/16972162-1239095354.html",
            "https://example.com/redirect?url=https://example.com/news",
            "https://example.com/jump/to?target=/news",
            "https://example.com/goto?to=https://example.com/news",
        ):
            with self.subTest(url=url):
                self.assertFalse(discover.looks_like_article_url(url))

    def test_include_exclude_patterns_override(self) -> None:
        include_config = CrawlConfig(
            entry_url="https://example.com/", include_patterns=[r"/special/"]
        )
        self.assertTrue(
            discover.looks_like_article_url("https://example.com/special/x", include_config)
        )
        exclude_config = CrawlConfig(
            entry_url="https://www.bbc.com/", exclude_patterns=[r"/articles/"]
        )
        self.assertFalse(
            discover.looks_like_article_url(
                "https://www.bbc.com/news/articles/cm8xz7pkd0jjo", exclude_config
            )
        )
        both = CrawlConfig(
            entry_url="https://example.com/",
            include_patterns=[r"/video/"],
            exclude_patterns=[r"/video/"],
        )
        self.assertFalse(discover.looks_like_article_url("https://example.com/video/1", both))


class ExtractFeedItemsTest(unittest.TestCase):
    def test_bbc_rss_fixture(self) -> None:
        fixture = load_fixture("bbc_rss")
        items = discover.extract_feed_items(fixture.text, fixture.url)
        self.assertGreaterEqual(len(items), 10)
        first = items[0]
        self.assertTrue(first.url.startswith("https://"))
        self.assertTrue(first.title)
        self.assertTrue(first.published_raw)
        self.assertEqual(first.method, "feed")
        for item in items[:5]:
            self.assertIn("bbc.co", item.url)

    def test_limit_argument(self) -> None:
        fixture = load_fixture("bbc_rss")
        self.assertEqual(len(discover.extract_feed_items(fixture.text, fixture.url, limit=3)), 3)

    def test_atom_feed(self) -> None:
        atom = """<?xml version="1.0" encoding="utf-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <title>Example Atom</title>
          <entry>
            <title>Atom entry one</title>
            <link rel="alternate" href="https://example.com/news/2026/10/29/atom-1"/>
            <published>2026-10-29T10:00:00Z</published>
            <category term="World"/>
          </entry>
          <entry>
            <title>Atom entry two</title>
            <link href="https://example.com/news/2026/10/29/atom-2"/>
            <updated>2026-10-29T11:00:00Z</updated>
          </entry>
        </feed>"""
        items = discover.extract_feed_items(atom, "https://example.com/feed.xml")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].url, "https://example.com/news/2026/10/29/atom-1")
        self.assertEqual(items[0].title, "Atom entry one")
        self.assertEqual(items[0].published_raw, "2026-10-29T10:00:00Z")
        self.assertEqual(items[0].section, "World")
        self.assertEqual(items[1].url, "https://example.com/news/2026/10/29/atom-2")

    def test_rss2_fields(self) -> None:
        rss = """<?xml version="1.0"?><rss version="2.0"><channel><title>Site</title>
        <item><title>First &amp; foremost</title>
          <link>https://example.com/news/2026/10/29/first</link>
          <pubDate>Thu, 29 Oct 2026 10:00:00 GMT</pubDate>
          <category>Politics</category>
          <enclosure url="https://example.com/img/a.jpg" type="image/jpeg"/>
        </item></channel></rss>"""
        items = discover.extract_feed_items(rss, "https://example.com/rss.xml")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "First & foremost")
        self.assertEqual(items[0].published_raw, "Thu, 29 Oct 2026 10:00:00 GMT")
        self.assertEqual(items[0].section, "Politics")
        self.assertEqual(items[0].image, "https://example.com/img/a.jpg")

    def test_json_feed_and_invalid_input(self) -> None:
        json_feed = json.dumps(
            {"version": "https://jsonfeed.org/version/1.1", "items": [
                {"id": "https://example.com/news/1", "title": "JSON item",
                 "date_published": "2026-10-29T10:00:00Z"},
            ]}
        )
        items = discover.extract_feed_items(json_feed, "https://example.com/feed.json")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "JSON item")
        for text in ("", "   ", "<rss><channel>", "not xml at all"):
            with self.subTest(text=text):
                self.assertEqual(discover.extract_feed_items(text), [])


class DiscoverFeedsTest(unittest.TestCase):
    def test_declared_alternate_link(self) -> None:
        # 自定义 feed 路径（不命中 /feed、/rss.xml 等默认后缀）验证“页面声明”生效；
        # 也防止把 .xml 扩展名误当文章 URL 拒绝（feed URL 允许任何路径/扩展名）。
        page = (
            '<html><head><link rel="alternate" type="application/rss+xml" '
            'href="/feeds/main.xml"></head><body></body></html>'
        )
        fetcher = StubFetcher({
            "https://example.com/news/": page,
            "https://example.com/feeds/main.xml": load_fixture("bbc_rss").text,
        })
        tree = dom.parse_html(page)
        feeds = discover.discover_feeds(
            fetcher, "https://example.com/news/", CrawlConfig(entry_url="https://example.com/news/"),
            tree=tree,
        )
        self.assertIn("https://example.com/feeds/main.xml", feeds)

    def test_profile_configured_feeds(self) -> None:
        profile = SiteProfile.from_dict({
            "domain": "example.com",
            "discovery": {"feeds": ["https://example.com/custom.xml"]},
        })
        fetcher = StubFetcher({})
        feeds = discover.discover_feeds(
            fetcher, "https://example.com/news/",
            CrawlConfig(entry_url="https://example.com/news/"),
            tree=dom.parse_html("<html><body></body></html>"),
            profile=profile,
        )
        self.assertIn("https://example.com/custom.xml", feeds)

    def test_suffix_probe(self) -> None:
        fetcher = StubFetcher({"https://example.com/feed": load_fixture("bbc_rss").text})
        feeds = discover.discover_feeds(
            fetcher, "https://example.com/news/", CrawlConfig(entry_url="https://example.com/news/")
        )
        self.assertEqual(feeds, ["https://example.com/feed"])

    def test_probe_disabled_only_returns_declared(self) -> None:
        fetcher = StubFetcher({"https://example.com/feed": load_fixture("bbc_rss").text})
        feeds = discover.discover_feeds(
            fetcher, "https://example.com/news/",
            CrawlConfig(entry_url="https://example.com/news/"), probe=False,
        )
        self.assertEqual(feeds, [])


class CandidateLinkSelectorsTest(unittest.TestCase):
    def test_bbc_list_fixture(self) -> None:
        tree = dom.parse_html(load_fixture("bbc_news_list").text)
        selectors = discover.candidate_link_selectors(tree)
        self.assertGreaterEqual(len(selectors), 3)
        for entry in selectors:
            self.assertEqual(set(entry), {"selector", "count", "sample_titles"})
            self.assertIsInstance(entry["count"], int)
            self.assertGreater(entry["count"], 0)
        counts = [entry["count"] for entry in selectors]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_min_count_filter_and_none_tree(self) -> None:
        tree = dom.parse_html(load_fixture("bbc_news_list").text)
        self.assertEqual(discover.candidate_link_selectors(tree, min_count=1000), [])
        self.assertEqual(discover.candidate_link_selectors(None), [])


class DiscoverNewsFeedTest(unittest.TestCase):
    def test_entry_is_rss(self) -> None:
        url = "https://feeds.bbci.co.uk/news/rss.xml"
        fetcher = StubFetcher({url: load_fixture("bbc_rss").text})
        result = discover.discover_news(
            fetcher, url, CrawlConfig(entry_url=url, max_articles=10, max_candidates=50)
        )
        self.assertEqual(result.method, "feed")
        self.assertGreaterEqual(len(result.items), 10)
        self.assertIn(url, result.feeds)
        self.assertTrue(all(item.method == "feed" for item in result.items))

    def test_feed_auto_discovery_from_list_page(self) -> None:
        fetcher = StubFetcher({
            "https://example.com/news/": FEED_DISCOVERY_PAGE,
            "https://example.com/rss.xml": load_fixture("bbc_rss").text,
        })
        result = discover.discover_news(
            fetcher, "https://example.com/news/",
            CrawlConfig(entry_url="https://example.com/news/", max_articles=10, max_candidates=50),
        )
        self.assertEqual(result.method, "feed")
        self.assertIn("https://example.com/rss.xml", result.feeds)
        self.assertGreaterEqual(len(result.items), 10)


class FeedItemFilteringTest(unittest.TestCase):
    def test_feed_items_filter_non_article_urls(self) -> None:
        """RSS item 里的根路径/频道首页不应成为候选（与启发式路径使用同一过滤规则）。"""
        list_page = (
            '<html><head><link rel="alternate" type="application/rss+xml" href="/rss.xml">'
            "</head><body><h1>News</h1></body></html>"
        )
        feed = """<?xml version="1.0"?><rss version="2.0"><channel><title>Site</title>
        <item><title>Site homepage</title><link>https://example.com/</link></item>
        <item><title>Channel page</title><link>https://example.com/news</link></item>
        <item><title>Real article one</title><link>https://example.com/c/2026-10-29/doc-abc1.shtml</link></item>
        <item><title>Real article two</title><link>https://example.com/c/2026-10-29/doc-abc2.shtml</link></item>
        </channel></rss>"""
        fetcher = StubFetcher({
            "https://example.com/news/": list_page,
            "https://example.com/rss.xml": feed,
        })
        result = discover.discover_news(
            fetcher, "https://example.com/news/",
            CrawlConfig(entry_url="https://example.com/news/", max_articles=10, max_candidates=50),
        )
        urls = [item.url for item in result.items]
        self.assertNotIn("https://example.com/", urls)
        self.assertNotIn("https://example.com/news", urls)
        self.assertTrue(all(discover.looks_like_article_url(url) for url in urls))
        self.assertEqual(len(urls), 2)

    def test_feed_redirect_endpoint_items_filtered(self) -> None:
        """RSS item 里的 redirect.php?url=... 跳转端点也应被过滤（新浪同款形态）。"""
        list_page = (
            '<html><head><link rel="alternate" type="application/rss+xml" '
            'href="https://feeds.example.com/rss.xml"></head><body></body></html>'
        )
        feed = """<?xml version="1.0"?><rss version="2.0"><channel><title>Site</title>
        <item><title>Redirect garbage</title>
          <link>http://go.example.com/redirect.php?url=http://you.video.example.com/b/1.html</link></item>
        <item><title>Real article one</title><link>https://example.com/c/2026-10-29/doc-abc1.shtml</link></item>
        <item><title>Real article two</title><link>https://example.com/c/2026-10-29/doc-abc2.shtml</link></item>
        </channel></rss>"""
        fetcher = StubFetcher({
            "https://example.com/news/": list_page,
            "https://feeds.example.com/rss.xml": feed,
        })
        result = discover.discover_news(
            fetcher, "https://example.com/news/",
            CrawlConfig(entry_url="https://example.com/news/", max_articles=10, max_candidates=50),
        )
        urls = [item.url for item in result.items]
        self.assertFalse(any("redirect.php" in url for url in urls))
        self.assertEqual(len(urls), 2)


class DiscoverNewsJsonLdTest(unittest.TestCase):
    def test_item_list_jsonld(self) -> None:
        fetcher = StubFetcher({"https://example.com/news/": JSONLD_PAGE})
        result = discover.discover_news(
            fetcher, "https://example.com/news/",
            CrawlConfig(entry_url="https://example.com/news/", max_articles=10, max_candidates=50),
        )
        self.assertEqual(result.method, "jsonld")
        self.assertEqual(len(result.items), 4)
        self.assertEqual(result.pages, ["https://example.com/news/"])
        self.assertEqual(
            [item.url for item in result.items],
            [f"https://example.com/news/2026/10/29/{suffix}" for suffix in "abcd"],
        )


class DiscoverNewsHeuristicTest(unittest.TestCase):
    def test_bbc_list_fixture(self) -> None:
        fetcher = StubFetcher({BBC_LIST_URL: load_fixture("bbc_news_list").text})
        config = CrawlConfig(entry_url=BBC_LIST_URL, max_articles=10, max_candidates=50)
        result = discover.discover_news(fetcher, BBC_LIST_URL, config)
        self.assertIn(result.method, ("heuristic", "mixed"))
        self.assertGreaterEqual(len(result.items), 5)
        for item in result.items:
            self.assertTrue(discover.looks_like_article_url(item.url))
            self.assertTrue(item.url.startswith("https://www.bbc.com/"))
            self.assertTrue(item.method)

    def test_synthetic_repeated_containers(self) -> None:
        page = heuristic_page("h", "2026-10-29", 6)
        fetcher = StubFetcher({"https://example.com/list/": page})
        result = discover.discover_news(
            fetcher, "https://example.com/list/",
            CrawlConfig(entry_url="https://example.com/list/", max_articles=10, max_candidates=50),
        )
        self.assertEqual(result.method, "heuristic")
        self.assertEqual(len(result.items), 6)
        self.assertTrue(all(item.method == "heuristic" for item in result.items))
        self.assertIn("新闻标题", result.items[0].title)

    def test_deduplicates_same_article_url(self) -> None:
        page = """<html><body><div class="list">
        <div class="item"><h2><a href="/article/2026/10/29/same?utm_source=x">重复文章标题需要足够长度的文本</a></h2></div>
        <div class="item"><h2><a href="/article/2026/10/29/same">重复文章标题需要足够长度的文本</a></h2></div>
        <div class="item"><h2><a href="/article/2026/10/29/other">另一篇不同的文章标题也需要足够长</a></h2></div>
        </div></body></html>"""
        fetcher = StubFetcher({"https://example.com/dup/": page})
        result = discover.discover_news(
            fetcher, "https://example.com/dup/",
            CrawlConfig(entry_url="https://example.com/dup/", max_articles=10, max_candidates=50),
        )
        self.assertEqual(len(result.items), 2)


class DiscoverNewsProfileTest(unittest.TestCase):
    def test_profile_items(self) -> None:
        fetcher = StubFetcher({"https://example.com/profile-list/": PROFILE_PAGE})
        profile = SiteProfile.from_dict({
            "domain": "example.com",
            "list": {"item": "ul#list li a::attr(href)", "item_title": "ul#list li a"},
        })
        result = discover.discover_news(
            fetcher, "https://example.com/profile-list/",
            CrawlConfig(entry_url="https://example.com/profile-list/", max_articles=10, max_candidates=50),
            profile=profile,
        )
        self.assertEqual(result.method, "profile")
        self.assertEqual(len(result.items), 4)
        self.assertTrue(all(item.method == "profile" for item in result.items))
        self.assertIn("Configured title number 1", result.items[0].title)
        self.assertTrue(result.items[0].url.startswith("https://example.com/news/2026/10/29/configured-1"))


class DiscoverNewsPaginationTest(unittest.TestCase):
    def test_follows_rel_next(self) -> None:
        page1 = (
            '<html><body><div class="list">'
            + "".join(
                f'<div class="item"><h2><a href="/article/2026/10/29/p1-{index}">'
                f"第一页第{index}条新闻标题，包含足够长度文本</a></h2></div>"
                for index in range(1, 5)
            )
            + '</div><a rel="next" href="/page2">下一页</a></body></html>'
        )
        page2 = heuristic_page("p2-", "2026-10-30", 3)
        fetcher = StubFetcher({
            "https://example.com/paged/": page1,
            "https://example.com/page2": page2,
        })
        result = discover.discover_news(
            fetcher, "https://example.com/paged/",
            CrawlConfig(
                entry_url="https://example.com/paged/",
                max_articles=10, max_candidates=50, max_list_pages=2,
            ),
        )
        self.assertEqual(len(result.pages), 2)
        self.assertEqual(result.pages[1], "https://example.com/page2")
        self.assertGreaterEqual(len(result.items), 6)
        urls = [item.url for item in result.items]
        self.assertTrue(any("p1-" in url for url in urls))
        self.assertTrue(any("p2-" in url for url in urls))


class DiscoverNewsFailureTest(unittest.TestCase):
    def test_fetch_failure_returns_empty_with_warning(self) -> None:
        fetcher = StubFetcher({})
        result = discover.discover_news(
            fetcher, "https://example.com/missing/",
            CrawlConfig(entry_url="https://example.com/missing/"),
        )
        self.assertEqual(result.items, [])
        self.assertEqual(result.method, "")
        self.assertTrue(result.warnings)
        self.assertIn("抓取失败", result.warnings[0])


class ProbeStructureTest(unittest.TestCase):
    def test_report_shape_and_size(self) -> None:
        fetcher = StubFetcher({
            BBC_LIST_URL: load_fixture("bbc_news_list").text,
        })
        report = discover.probe_structure(fetcher, BBC_LIST_URL, CrawlConfig(entry_url=BBC_LIST_URL))
        self.assertEqual(
            set(report),
            {
                "url", "title", "language", "feeds", "jsonld_types",
                "candidate_selectors", "article_links_sample", "pagination_hint",
                "article_page_probe", "error",
            },
        )
        self.assertEqual(report["error"], "")
        self.assertEqual(report["url"], BBC_LIST_URL)
        self.assertTrue(report["title"])
        self.assertTrue(report["language"])
        self.assertGreaterEqual(len(report["candidate_selectors"]), 1)
        self.assertGreaterEqual(len(report["article_links_sample"]), 1)
        for link in report["article_links_sample"]:
            self.assertIn("url", link)
            self.assertIn("title", link)
        payload = json.dumps(report, ensure_ascii=False)
        self.assertLess(len(payload), 8192)

    def test_probe_failure_is_reported_not_raised(self) -> None:
        report = discover.probe_structure(
            StubFetcher({}), "https://example.com/missing/",
            CrawlConfig(entry_url="https://example.com/missing/"),
        )
        self.assertTrue(report["error"])
        self.assertEqual(report["url"], "https://example.com/missing/")


class DiscoveryResultTest(unittest.TestCase):
    def test_defaults(self) -> None:
        result = discover.DiscoveryResult()
        self.assertEqual(result.items, [])
        self.assertEqual(result.method, "")
        self.assertEqual(result.pages, [])
        self.assertEqual(result.feeds, [])
        self.assertEqual(result.warnings, [])


if __name__ == "__main__":
    unittest.main()
