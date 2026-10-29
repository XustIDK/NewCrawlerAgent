# -*- coding: utf-8 -*-
"""news_agent.normalize 单元测试：多语言日期、语言识别、canonical URL、文本清洗、CJK 词数。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from news_agent.normalize import (
    canonicalize_url,
    clean_text,
    detect_language,
    first_sentences,
    normalize_lang_code,
    parse_datetime,
    truncate,
    word_count,
)

NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)


class ParseDatetimeTest(unittest.TestCase):
    """多语言日期 -> ISO 8601；表驱动，失败项带原始输入便于定位。"""

    def check(self, raw: str, expected: str, **kwargs: object) -> None:
        kwargs.setdefault("now", NOW)
        self.assertEqual(parse_datetime(raw, **kwargs), expected)  # type: ignore[arg-type]

    def test_iso8601_variants(self) -> None:
        cases = [
            ("2026-10-29T10:30:00+00:00", "2026-10-29T10:30:00+00:00"),
            ("2026-10-29T10:30:00Z", "2026-10-29T10:30:00+00:00"),
            ("2026-10-29T10:30:00+08:00", "2026-10-29T10:30:00+08:00"),
            ("2026-10-29 10:30:00", "2026-10-29T10:30:00+00:00"),
            ("2026-10-29", "2026-10-29T00:00:00+00:00"),
            ("20261029", "2026-10-29T00:00:00+00:00"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.check(raw, expected)

    def test_rfc2822(self) -> None:
        self.check("Thu, 29 Oct 2026 10:30:00 GMT", "2026-10-29T10:30:00+00:00")
        self.check("Thu, 29 Oct 2026 10:30:00 +0800", "2026-10-29T10:30:00+08:00")

    def test_english_month_names(self) -> None:
        self.check("October 29, 2026", "2026-10-29T00:00:00+00:00")
        self.check("29 October 2026, 10:30", "2026-10-29T10:30:00+00:00")
        self.check("Oct 29, 2026 10:30 PM", "2026-10-29T22:30:00+00:00")

    def test_chinese_dates(self) -> None:
        self.check("2026年10月29日", "2026-10-29T00:00:00+00:00")
        self.check("2026年10月29日 10:30", "2026-10-29T10:30:00+00:00")
        self.check("发布时间：2026-10-29 10:30", "2026-10-29T10:30:00+00:00")
        self.check("2026年10月29日 下午3:05", "2026-10-29T15:05:00+00:00")

    def test_japanese_dates(self) -> None:
        self.check("2026年10月29日 10時30分", "2026-10-29T10:30:00+09:00",
                   default_timezone="Asia/Tokyo")
        self.check("2026年10月29日 10:30", "2026-10-29T10:30:00+00:00")

    def test_european_month_names(self) -> None:
        self.check("29. Oktober 2026", "2026-10-29T00:00:00+00:00")
        self.check("29 octobre 2026", "2026-10-29T00:00:00+00:00")
        self.check("29 de octubre de 2026", "2026-10-29T00:00:00+00:00")
        self.check("29 октября 2026", "2026-10-29T00:00:00+00:00")
        self.check("29 luglio 2026", "2026-07-29T00:00:00+00:00")
        self.check("29 juli 2026", "2026-07-29T00:00:00+00:00")

    def test_relative_times(self) -> None:
        self.check("2 hours ago", "2026-10-29T10:00:00+00:00")
        self.check("30 minutes ago", "2026-10-29T11:30:00+00:00")
        self.check("3 days ago", "2026-10-26T12:00:00+00:00")
        self.check("3天前", "2026-10-26T12:00:00+00:00")
        self.check("2小时前", "2026-10-29T10:00:00+00:00")
        self.check("vor 2 Stunden", "2026-10-29T10:00:00+00:00")
        self.check("il y a 2 heures", "2026-10-29T10:00:00+00:00")
        self.check("hace 2 horas", "2026-10-29T10:00:00+00:00")
        self.check("2時間前", "2026-10-29T10:00:00+00:00")

    def test_relative_day_names(self) -> None:
        self.check("yesterday", "2026-10-28T00:00:00+00:00")
        self.check("today", "2026-10-29T00:00:00+00:00")
        self.check("昨天", "2026-10-28T00:00:00+00:00")
        self.check("今日", "2026-10-29T00:00:00+00:00")
        self.check("gestern", "2026-10-28T00:00:00+00:00")

    def test_epoch_seconds_and_milliseconds(self) -> None:
        self.check("1700000000", "2023-11-14T22:13:20+00:00")
        self.check("1700000000000", "2023-11-14T22:13:20+00:00")

    def test_default_timezone_applied_to_naive_input(self) -> None:
        self.check(
            "2026-10-29 10:30:00",
            "2026-10-29T10:30:00+08:00",
            default_timezone="Asia/Shanghai",
        )

    def test_numeric_date_language_disambiguation(self) -> None:
        self.check("10/29/2026", "2026-10-29T00:00:00+00:00")
        self.check("29/10/2026", "2026-10-29T00:00:00+00:00")
        self.check("03/04/2026", "2026-04-03T00:00:00+00:00")
        self.check("03/04/2026", "2026-03-04T00:00:00+00:00", language="en")

    def test_invalid_inputs_return_empty_string(self) -> None:
        for raw in ("", "   ", "not a date", "2026-13-45", "99/99/9999", "hello 123", None):
            with self.subTest(raw=raw):
                self.assertEqual(parse_datetime(raw), "")  # type: ignore[arg-type]

    def test_datetime_object_input(self) -> None:
        value = datetime(2026, 10, 29, 10, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_datetime(value), "2026-10-29T10:30:00+00:00")  # type: ignore[arg-type]


class CanonicalizeUrlTest(unittest.TestCase):
    def test_lowercases_host_removes_fragment_tracking_and_trailing_slash(self) -> None:
        self.assertEqual(
            canonicalize_url("HTTPS://WWW.BBC.COM:443/news/story/?utm_source=x#frag"),
            "https://www.bbc.com/news/story",
        )

    def test_removes_default_ports_and_collapses_slashes(self) -> None:
        self.assertEqual(canonicalize_url("http://example.com:80/a"), "http://example.com/a")
        self.assertEqual(canonicalize_url("https://example.com/a//b"), "https://example.com/a/b")

    def test_sorts_remaining_query(self) -> None:
        self.assertEqual(
            canonicalize_url("https://x.com/a?b=2&a=1&utm_medium=x"),
            "https://x.com/a?a=1&b=2",
        )

    def test_relative_url_uses_base(self) -> None:
        self.assertEqual(
            canonicalize_url("../story", "https://example.com/news/2026/"),
            "https://example.com/news/story",
        )

    def test_root_path_kept(self) -> None:
        self.assertEqual(canonicalize_url("https://example.com/"), "https://example.com/")

    def test_idn_host_encoded(self) -> None:
        self.assertEqual(
            canonicalize_url("https://www.例子.测试/新闻"),
            "https://www.xn--fsqu00a.xn--0zwm56d/新闻",
        )

    def test_invalid_urls_return_empty(self) -> None:
        for raw in ("", "   ", "mailto:a@b.com", "javascript:void(0)", "example.com", None):
            with self.subTest(raw=raw):
                self.assertEqual(canonicalize_url(raw), "")  # type: ignore[arg-type]


class CleanTextTest(unittest.TestCase):
    def test_entities_nbsp_and_whitespace(self) -> None:
        self.assertEqual(clean_text("Fish &amp; Chips"), "Fish & Chips")
        self.assertEqual(clean_text("&lt;b&gt;bold&lt;/b&gt;"), "<b>bold</b>")
        self.assertEqual(clean_text("&amp;#39;"), "'")
        self.assertEqual(clean_text("Tom &amp;amp; Jerry"), "Tom & Jerry")
        self.assertEqual(clean_text("a\u00a0b\u3000c\u200bd"), "a b cd")
        self.assertEqual(clean_text("  a \n\t b  "), "a b")

    def test_strips_decorative_edges_and_normalizes_nfc(self) -> None:
        self.assertEqual(clean_text("| headline |"), "headline")
        self.assertEqual(clean_text("· 标题 ·"), "标题")
        self.assertEqual(clean_text("e\u0301"), "é")

    def test_empty_and_non_string(self) -> None:
        self.assertEqual(clean_text(None), "")  # type: ignore[arg-type]
        self.assertEqual(clean_text(""), "")
        self.assertEqual(clean_text(123), "123")  # type: ignore[arg-type]


class TruncateTest(unittest.TestCase):
    def test_short_text_unchanged(self) -> None:
        self.assertEqual(truncate("short", 10), "short")

    def test_long_text_limited_and_ellipsis(self) -> None:
        result = truncate("a" * 100, 10)
        self.assertEqual(len(result), 10)
        self.assertTrue(result.endswith("…"))

    def test_sentence_boundary_preferred(self) -> None:
        result = truncate("First sentence. Second sentence is long.", 25)
        self.assertLessEqual(len(result), 25)
        self.assertTrue(result.startswith("First sentence."))
        self.assertTrue(result.endswith("…"))

    def test_cjk_and_zero_limit(self) -> None:
        self.assertEqual(truncate("第一句话。第二句话很长。", 5), "第一句话…")
        self.assertEqual(truncate("abc", 0), "…")

    def test_empty_input(self) -> None:
        self.assertEqual(truncate("", 10), "")
        self.assertEqual(truncate(None, 10), "")  # type: ignore[arg-type]


class WordCountTest(unittest.TestCase):
    def test_english_words(self) -> None:
        self.assertEqual(word_count("Hello world, this is a test."), 6)
        self.assertEqual(word_count("don't stop"), 2)

    def test_cjk_counted_per_char(self) -> None:
        self.assertEqual(word_count("你好世界"), 4)
        self.assertEqual(word_count("こんにちは世界"), 7)

    def test_mixed_and_empty(self) -> None:
        self.assertEqual(word_count("你好 world"), 3)
        self.assertEqual(word_count(""), 0)
        self.assertEqual(word_count(None), 0)  # type: ignore[arg-type]


class FirstSentencesTest(unittest.TestCase):
    def test_short_text_returned_cleaned(self) -> None:
        self.assertEqual(first_sentences("Short text."), "Short text.")
        self.assertEqual(first_sentences(""), "")

    def test_stops_after_first_sentence_when_limit_small(self) -> None:
        self.assertEqual(
            first_sentences("First sentence. Second sentence.", 25),
            "First sentence.",
        )
        self.assertEqual(first_sentences("第一句话。第二句话。第三句话。", 8), "第一句话。")

    def test_long_text_truncated(self) -> None:
        result = first_sentences("A" * 300, 100)
        self.assertLessEqual(len(result), 100)


class NormalizeLangCodeTest(unittest.TestCase):
    def test_common_codes(self) -> None:
        cases = {
            "en-US": "en",
            "zh-Hans": "zh",
            "ZH_CN": "zh",
            "de-DE": "de",
            "fr-FR": "fr",
            "jp": "ja",
            "eng": "en",
            "und": "",
            "": "",
            "x": "",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_lang_code(raw), expected)


class DetectLanguageTest(unittest.TestCase):
    def test_html_lang_has_priority(self) -> None:
        self.assertEqual(detect_language("", "en-US"), "en")
        self.assertEqual(detect_language("bonjour", "fr"), "fr")

    def test_script_based_detection(self) -> None:
        cases = {
            "日本のニュースです。今日は晴天です。": "ja",
            "这是一条中文新闻，今天天气很好。": "zh",
            "한국 뉴스입니다. 오늘 날씨가 좋습니다.": "ko",
            "Сегодня в Москве прошла встреча глав государств.": "ru",
        }
        for text, expected in cases.items():
            with self.subTest(lang=expected):
                self.assertEqual(detect_language(text), expected)

    def test_latin_stopwords_detection(self) -> None:
        cases = {
            "The government said the new policy will be implemented next year.": "en",
            "Die Regierung sagte, dass die neue Politik im nächsten Jahr umgesetzt wird.": "de",
            "La France et l'Allemagne ont signé un accord sur la politique énergétique.": "fr",
            "El gobierno español anunció hoy que la nueva política se aplicará el próximo año.": "es",
        }
        for text, expected in cases.items():
            with self.subTest(lang=expected):
                self.assertEqual(detect_language(text), expected)

    def test_unknown_short_text(self) -> None:
        self.assertEqual(detect_language(""), "")
        self.assertEqual(detect_language("hello world"), "")


if __name__ == "__main__":
    unittest.main()
