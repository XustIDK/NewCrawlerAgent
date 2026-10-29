# -*- coding: utf-8 -*-
"""news_agent.models 单元测试：Article 字段契约、SiteProfile 默认合并、CrawlConfig 校验、去重 id。"""

from __future__ import annotations

import re
import unittest

from news_agent.models import (
    ARTICLE_FIELDS,
    DEFAULT_ARTICLE,
    DEFAULT_DISCOVERY,
    DEFAULT_FETCH,
    DEFAULT_LIST,
    DEFAULT_USER_AGENT,
    Article,
    CrawlConfig,
    CrawlResult,
    CrawlStats,
    NewsItem,
    SiteProfile,
    content_id,
    utc_now_iso,
)


class ContentIdTest(unittest.TestCase):
    def test_stable_16_hex_chars(self) -> None:
        first = content_id("https://example.com/news/story")
        second = content_id("https://example.com/news/story")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)
        self.assertRegex(first, r"^[0-9a-f]{16}$")

    def test_ignores_surrounding_whitespace(self) -> None:
        self.assertEqual(content_id(" https://example.com/a "), content_id("https://example.com/a"))

    def test_different_urls_differ(self) -> None:
        self.assertNotEqual(content_id("https://example.com/a"), content_id("https://example.com/b"))


class UtcNowIsoTest(unittest.TestCase):
    def test_iso8601_with_utc_offset(self) -> None:
        value = utc_now_iso()
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")


class ArticleTest(unittest.TestCase):
    def test_defaults_and_derived_fields(self) -> None:
        article = Article(url="https://www.example.com/news/story")
        self.assertEqual(article.site_domain, "example.com")
        self.assertEqual(article.canonical_url, article.url)
        self.assertTrue(article.crawled_at)
        self.assertEqual(article.images, [])
        self.assertEqual(article.confidence, 0.0)
        self.assertEqual(article.id, content_id(article.url))

    def test_word_count_defaults_to_body_length(self) -> None:
        article = Article(url="https://example.com/a", body="abc def")
        self.assertEqual(article.word_count, len("abc def"))

    def test_explicit_word_count_kept(self) -> None:
        self.assertEqual(Article(url="https://example.com/a", body="abc", word_count=42).word_count, 42)

    def test_id_prefers_canonical_url(self) -> None:
        article = Article(
            url="https://example.com/a?utm_source=x",
            canonical_url="https://example.com/a",
        )
        self.assertEqual(article.id, content_id("https://example.com/a"))

    def test_to_dict_has_stable_schema_keys(self) -> None:
        article = Article(url="https://example.com/a", title="T", body="B" * 5)
        data = article.to_dict()
        self.assertEqual(list(data.keys()), list(ARTICLE_FIELDS))
        self.assertEqual(data["id"], article.id)
        self.assertEqual(data["title"], "T")
        self.assertIsInstance(data["images"], list)

    def test_is_usable(self) -> None:
        usable = Article(url="https://example.com/a", title="Title", body="x" * 150)
        self.assertTrue(usable.is_usable())
        self.assertFalse(Article(url="https://example.com/a", title="", body="x" * 150).is_usable())
        self.assertFalse(Article(url="https://example.com/a", title="T", body="short").is_usable())
        self.assertTrue(Article(url="https://example.com/a", title="T", body="x").is_usable(min_body_chars=1))

    def test_completeness(self) -> None:
        empty = Article(url="https://example.com/a").completeness()
        self.assertEqual(set(empty), {"title", "body", "published_at", "author", "source", "language"})
        self.assertFalse(any(empty.values()))
        filled = Article(
            url="https://example.com/a",
            title="T",
            body="B",
            published_at="2026-10-29T10:00:00+00:00",
            author="A",
            source="S",
            language="en",
        ).completeness()
        self.assertTrue(all(filled.values()))


class NewsItemTest(unittest.TestCase):
    def test_to_dict_fields(self) -> None:
        item = NewsItem(url="https://example.com/a", title="T", list_url="https://example.com/")
        data = item.to_dict()
        self.assertEqual(
            set(data),
            {"url", "title", "published_raw", "section", "image", "list_url", "method"},
        )
        self.assertEqual(data["method"], "generic")


class SiteProfileTest(unittest.TestCase):
    def test_domain_normalized_and_defaults_merged(self) -> None:
        profile = SiteProfile(domain="WWW.BBC.COM")
        self.assertEqual(profile.domain, "bbc.com")
        self.assertEqual(profile.fetch, DEFAULT_FETCH)
        self.assertEqual(profile.discovery, DEFAULT_DISCOVERY)
        self.assertEqual(profile.list, DEFAULT_LIST)
        self.assertEqual(profile.article, DEFAULT_ARTICLE)
        self.assertTrue(profile.updated_at)

    def test_requires_domain(self) -> None:
        for domain in ("", "   "):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                SiteProfile(domain=domain)

    def test_from_dict_merges_partial_config(self) -> None:
        profile = SiteProfile.from_dict(
            {
                "domain": "news.example.com",
                "name": "Example News",
                "article": {"body": "article p"},
                "list": {"pagination": {"max_pages": 5}},
            }
        )
        self.assertEqual(profile.name, "Example News")
        self.assertEqual(profile.article["body"], "article p")
        self.assertEqual(profile.article["title"], "")
        self.assertEqual(profile.article["exclude"], [])
        self.assertEqual(profile.list["pagination"]["max_pages"], 5)
        self.assertEqual(profile.list["pagination"]["next"], "")

    def test_from_dict_ignores_unknown_keys_and_rejects_non_dict(self) -> None:
        profile = SiteProfile.from_dict({"domain": "x.com", "bogus": 1})
        self.assertFalse(hasattr(profile, "bogus"))
        with self.assertRaises(ValueError):
            SiteProfile.from_dict(["not", "a", "dict"])  # type: ignore[arg-type]

    def test_selector_and_group_helpers(self) -> None:
        profile = SiteProfile.from_dict({"domain": "x.com", "article": {"body": "article p"}})
        self.assertEqual(profile.selector("article", "body"), "article p")
        self.assertEqual(profile.selector("article", "missing"), "")
        self.assertEqual(profile.group("fetch")["mode"], "auto")

    def test_to_dict_roundtrip(self) -> None:
        profile = SiteProfile.from_dict({"domain": "x.com", "article": {"title": "h1"}})
        restored = SiteProfile.from_dict(profile.to_dict())
        self.assertEqual(restored.to_dict(), profile.to_dict())

    def test_instances_do_not_share_mutable_defaults(self) -> None:
        first = SiteProfile(domain="a.com")
        second = SiteProfile(domain="b.com")
        first.article["exclude"].append(".ad")
        first.include.append("x")
        self.assertEqual(second.article["exclude"], [])
        self.assertEqual(second.include, [])


class CrawlConfigTest(unittest.TestCase):
    def test_entry_url_normalized_and_domain(self) -> None:
        config = CrawlConfig(entry_url="bbc.com/news")
        self.assertEqual(config.entry_url, "https://bbc.com/news")
        self.assertEqual(config.domain, "bbc.com")
        self.assertEqual(config.user_agent, DEFAULT_USER_AGENT)

    def test_limits_are_clamped(self) -> None:
        config = CrawlConfig(entry_url="https://x.com/", max_articles=0, max_candidates=0)
        self.assertEqual(config.max_articles, 1)
        self.assertGreaterEqual(config.max_candidates, config.max_articles)
        self.assertEqual(CrawlConfig(entry_url="https://x.com/", max_articles=5, max_candidates=2).max_candidates, 5)

    def test_browser_mode_normalized(self) -> None:
        self.assertEqual(CrawlConfig(entry_url="https://x.com/", browser_mode="ALWAYS").browser_mode, "always")
        self.assertEqual(CrawlConfig(entry_url="https://x.com/", browser_mode="chrome").browser_mode, "auto")
        self.assertEqual(CrawlConfig(entry_url="https://x.com/", browser_mode="").browser_mode, "auto")

    def test_from_settings_merges_and_overrides(self) -> None:
        settings = {
            "crawl": {"max_articles": 6, "timeout_s": 5, "retries": 1},
            "llm": {"max_rounds": 9},
            "user_agent": "Test-UA",
            "output": {"dir": "custom_out"},
        }
        config = CrawlConfig.from_settings("example.com/news", settings, max_articles=2)
        self.assertEqual(config.max_articles, 2)
        self.assertEqual(config.timeout_s, 5)
        self.assertEqual(config.retries, 1)
        self.assertEqual(config.max_rounds, 9)
        self.assertEqual(config.user_agent, "Test-UA")
        self.assertEqual(config.out_dir, "custom_out")
        self.assertEqual(config.entry_url, "https://example.com/news")

    def test_from_settings_defaults(self) -> None:
        config = CrawlConfig.from_settings("example.com", {})
        self.assertEqual(config.max_articles, 10)
        self.assertEqual(config.max_rounds, 12)
        self.assertEqual(config.user_agent, DEFAULT_USER_AGENT)
        self.assertEqual(config.out_dir, "data")

    def test_none_override_keeps_settings_value(self) -> None:
        config = CrawlConfig.from_settings(
            "example.com", {"crawl": {"max_articles": 6}}, max_articles=None
        )
        self.assertEqual(config.max_articles, 6)

    def test_to_dict_exposes_fields(self) -> None:
        data = CrawlConfig(entry_url="https://x.com/").to_dict()
        self.assertEqual(data["entry_url"], "https://x.com/")
        self.assertNotIn("domain", data)


class CrawlStatsTest(unittest.TestCase):
    def test_note_method_buckets(self) -> None:
        stats = CrawlStats()
        stats.note_method("discovery", "feed")
        stats.note_method("discovery", "feed")
        stats.note_method("extract", "jsonld")
        self.assertEqual(stats.discovery_methods, {"feed": 2})
        self.assertEqual(stats.extract_methods, {"jsonld": 1})

    def test_error_and_warning_caps(self) -> None:
        stats = CrawlStats()
        for index in range(60):
            stats.add_error(f"https://x.com/{index}", "fetch", "x" * 800)
        self.assertEqual(len(stats.errors), 50)
        self.assertEqual(len(stats.errors[0]["error"]), 500)
        for index in range(60):
            stats.add_warning(f"warning-{index}")
        stats.add_warning("warning-0")
        self.assertEqual(len(stats.warnings), 50)

    def test_finish_sets_timestamp(self) -> None:
        stats = CrawlStats()
        self.assertTrue(stats.started_at)
        self.assertEqual(stats.finished_at, "")
        stats.finish()
        self.assertTrue(stats.finished_at)
        self.assertIn("finished_at", stats.to_dict())


class CrawlResultTest(unittest.TestCase):
    def test_to_dict_serializes_articles(self) -> None:
        result = CrawlResult(entry_url="https://x.com/", domain="x.com")
        article = Article(url="https://x.com/a", title="T", body="B")
        result.add_article(article)
        result.warnings.append("w")
        data = result.to_dict()
        self.assertEqual(data["article_count"], 1)
        self.assertEqual(data["articles"][0]["id"], article.id)
        self.assertEqual(data["warnings"], ["w"])
        self.assertIn("stats", data)
        self.assertEqual(data["domain"], "x.com")


if __name__ == "__main__":
    unittest.main()
