# -*- coding: utf-8 -*-
"""news_agent.urls 单元测试：入口 URL 补全/校验、域名归一、追踪参数、同站判断、正则匹配。"""

from __future__ import annotations

import unittest

from news_agent.urls import (
    absolute_url,
    domain_from_url,
    is_http_url,
    is_same_site,
    normalize_entry_url,
    site_root,
    strip_tracking,
    url_matches_any,
)


class NormalizeEntryUrlTest(unittest.TestCase):
    def test_adds_https_when_scheme_missing(self) -> None:
        self.assertEqual(normalize_entry_url("example.com/news"), "https://example.com/news")

    def test_domain_only_becomes_root_path(self) -> None:
        self.assertEqual(normalize_entry_url("bbc.com"), "https://bbc.com/")

    def test_keeps_query_removes_fragment_and_spaces(self) -> None:
        self.assertEqual(
            normalize_entry_url("  https://www.bbc.com/news?page=2#top  "),
            "https://www.bbc.com/news?page=2",
        )

    def test_keeps_unicode_path(self) -> None:
        self.assertEqual(
            normalize_entry_url("https://news.example.com/中文"),
            "https://news.example.com/中文",
        )

    def test_allows_http_scheme(self) -> None:
        self.assertEqual(normalize_entry_url("http://example.com/a"), "http://example.com/a")

    def test_rejects_empty_input(self) -> None:
        for raw in ("", "   "):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize_entry_url(raw)

    def test_rejects_non_http_scheme(self) -> None:
        for raw in (
            "ftp://example.com/a",
            "javascript:alert(1)",
            "mailto:someone@example.com",
            "file:///c:/temp/a.html",
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize_entry_url(raw)

    def test_rejects_missing_host(self) -> None:
        with self.assertRaises(ValueError):
            normalize_entry_url("https://")

    def test_rejects_non_string(self) -> None:
        with self.assertRaises(ValueError):
            normalize_entry_url(None)  # type: ignore[arg-type]


class DomainFromUrlTest(unittest.TestCase):
    def test_strips_www_and_lowercases(self) -> None:
        self.assertEqual(domain_from_url("https://WWW.BBC.COM/news"), "bbc.com")

    def test_keeps_other_subdomain(self) -> None:
        self.assertEqual(domain_from_url("https://m.bbc.com/x"), "m.bbc.com")

    def test_strips_port_userinfo_and_trailing_dot(self) -> None:
        self.assertEqual(domain_from_url("https://user:pw@WWW.BBC.COM.:8080/x"), "bbc.com")

    def test_idn_to_ascii(self) -> None:
        self.assertEqual(
            domain_from_url("https://www.例子.测试/新闻"),
            "xn--fsqu00a.xn--0zwm56d",
        )

    def test_invalid_or_empty(self) -> None:
        for raw in ("", "   ", "not a url"):
            with self.subTest(raw=raw):
                self.assertEqual(domain_from_url(raw), "")


class SiteRootTest(unittest.TestCase):
    def test_returns_scheme_and_netloc(self) -> None:
        self.assertEqual(site_root("https://example.com/news/x?q=1#f"), "https://example.com")
        self.assertEqual(site_root("http://www.example.com"), "http://www.example.com")


class AbsoluteUrlTest(unittest.TestCase):
    def test_relative_and_absolute(self) -> None:
        base = "https://example.com/news/"
        self.assertEqual(absolute_url(base, "/story/1"), "https://example.com/story/1")
        self.assertEqual(absolute_url(base, "story/1"), "https://example.com/news/story/1")
        self.assertEqual(
            absolute_url(base, "https://other.com/x?y=1#f"),
            "https://other.com/x?y=1",
        )
        self.assertEqual(
            absolute_url(base, "//cdn.example.com/img.jpg"),
            "https://cdn.example.com/img.jpg",
        )

    def test_invalid_links_are_filtered(self) -> None:
        base = "https://example.com/news/"
        for href in (
            "",
            "#top",
            "javascript:void(0)",
            "mailto:someone@example.com",
            "tel:123456",
            "data:text/plain,hello",
            "ftp://example.com/file",
        ):
            with self.subTest(href=href):
                self.assertEqual(absolute_url(base, href), "")


class StripTrackingTest(unittest.TestCase):
    def test_removes_tracking_parameters(self) -> None:
        self.assertEqual(
            strip_tracking("https://x.com/a?utm_source=wx&id=3&fbclid=abc&spm=1.2"),
            "https://x.com/a?id=3",
        )

    def test_sorts_remaining_parameters(self) -> None:
        self.assertEqual(strip_tracking("https://x.com/a?b=2&a=1#top"), "https://x.com/a?a=1&b=2")

    def test_only_tracking_parameters(self) -> None:
        self.assertEqual(strip_tracking("https://x.com/a?utm_source="), "https://x.com/a")

    def test_url_without_query_unchanged(self) -> None:
        self.assertEqual(strip_tracking("https://x.com/a"), "https://x.com/a")


class SameSiteTest(unittest.TestCase):
    def test_same_domain_and_subdomain(self) -> None:
        self.assertTrue(is_same_site("https://bbc.com/news", "bbc.com"))
        self.assertTrue(is_same_site("https://www.bbc.com/news", "bbc.com"))
        self.assertTrue(is_same_site("https://m.bbc.com/news", "bbc.com"))

    def test_other_domains_are_rejected(self) -> None:
        self.assertFalse(is_same_site("https://notbbc.com/news", "bbc.com"))
        self.assertFalse(is_same_site("https://bbc.com.evil.com/news", "bbc.com"))
        self.assertFalse(is_same_site("https://x.com/news", ""))

    def test_idn_domain(self) -> None:
        self.assertTrue(is_same_site("https://www.例子.测试/x", "xn--fsqu00a.xn--0zwm56d"))


class UrlMatchesAnyTest(unittest.TestCase):
    def test_matches_path_plus_query(self) -> None:
        url = "https://x.com/2026/10/29/story.html?utm_source=1"
        self.assertTrue(url_matches_any(url, [r"/2026/\d{2}/\d{2}/"]))
        self.assertTrue(url_matches_any("https://x.com/a?page=2", [r"page=\d"]))
        self.assertTrue(url_matches_any("https://x.com/news/VIDEO/a", ["/video/"]))

    def test_no_match_and_empty_patterns(self) -> None:
        self.assertFalse(url_matches_any("https://x.com/news/a", ["/video/"]))
        self.assertFalse(url_matches_any("https://x.com/news/a", []))

    def test_invalid_regex_falls_back_to_substring(self) -> None:
        self.assertTrue(url_matches_any("https://x.com/a?q=(1)", ["("]))
        self.assertFalse(url_matches_any("https://x.com/a?q=1", ["("]))


if __name__ == "__main__":
    unittest.main()
