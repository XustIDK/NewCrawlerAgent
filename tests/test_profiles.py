# -*- coding: utf-8 -*-
"""news_agent.profiles 单元测试：读写校验、域名/子域匹配、非法配置、模板。"""

from __future__ import annotations

import json
import tempfile
import unittest
import warnings
from pathlib import Path

from news_agent.errors import ProfileError
from news_agent.models import SiteProfile
from news_agent.profiles import (
    list_profiles,
    load_profile,
    load_profile_for_url,
    normalize_domain,
    profile_path,
    profile_template,
    save_profile,
    validate_profile,
)


class NormalizeDomainTest(unittest.TestCase):
    def test_url_and_plain_domain(self) -> None:
        self.assertEqual(normalize_domain("https://WWW.BBC.COM/news"), "bbc.com")
        self.assertEqual(normalize_domain("bbc.com"), "bbc.com")
        self.assertEqual(normalize_domain("news.bbc.com"), "news.bbc.com")
        self.assertEqual(
            normalize_domain("https://例子.测试/新闻"), "xn--fsqu00a.xn--0zwm56d"
        )

    def test_invalid(self) -> None:
        for value in ("", "   ", None):
            with self.subTest(value=value):
                self.assertEqual(normalize_domain(value), "")  # type: ignore[arg-type]


class ValidateProfileTest(unittest.TestCase):
    def test_minimal_valid_profile(self) -> None:
        profile = validate_profile({"domain": "WWW.BBC.COM", "name": "BBC"})
        self.assertIsInstance(profile, SiteProfile)
        self.assertEqual(profile.domain, "bbc.com")
        self.assertEqual(profile.name, "BBC")
        self.assertEqual(profile.article["body"], "")

    def test_missing_or_invalid_domain(self) -> None:
        for data in ({}, {"domain": ""}, {"domain": 123}, ["not", "dict"]):
            with self.subTest(data=data), self.assertRaises(ProfileError):
                validate_profile(data)  # type: ignore[arg-type]

    def test_bad_selector_types(self) -> None:
        cases = [
            {"domain": "x.com", "article": "h1"},
            {"domain": "x.com", "article": {"body": 123}},
            {"domain": "x.com", "article": {"exclude": ".ad"}},
            {"domain": "x.com", "list": {"pagination": "next"}},
            {"domain": "x.com", "list": {"pagination": {"max_pages": "3"}}},
            {"domain": "x.com", "include": "video"},
            {"domain": "x.com", "name": 123},
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ProfileError):
                validate_profile(data)  # type: ignore[arg-type]

    def test_unknown_keys_ignored(self) -> None:
        profile = validate_profile({"domain": "x.com", "bogus": 1})
        self.assertFalse(hasattr(profile, "bogus"))


class ProfilePathTest(unittest.TestCase):
    def test_path_uses_normalized_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = profile_path("https://www.bbc.com/news", tmp)
            self.assertEqual(path, Path(tmp) / "bbc.com.json")

    def test_invalid_domain_raises(self) -> None:
        with self.assertRaises(ProfileError):
            profile_path("")


class SaveLoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, filename: str, data: object) -> Path:
        path = self.dir / filename
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_save_and_load_roundtrip(self) -> None:
        profile = validate_profile(
            {"domain": "bbc.com", "name": "BBC News", "article": {"body": "article p"}}
        )
        path = save_profile(profile, self.dir)
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, "bbc.com.json")
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["domain"], "bbc.com")
        loaded = load_profile("www.bbc.com", self.dir)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.name, "BBC News")
        self.assertEqual(loaded.article["body"], "article p")
        self.assertTrue(loaded.updated_at)

    def test_save_dict_and_invalid_type(self) -> None:
        path = save_profile({"domain": "example.com", "name": "Example"}, self.dir)
        self.assertTrue(path.is_file())
        with self.assertRaises(ProfileError):
            save_profile("not-a-profile", self.dir)  # type: ignore[arg-type]

    def test_longest_domain_match_wins(self) -> None:
        save_profile({"domain": "bbc.com", "name": "BBC"}, self.dir)
        save_profile({"domain": "news.bbc.com", "name": "BBC News"}, self.dir)
        self.assertEqual(load_profile("www.bbc.com", self.dir).name, "BBC")  # type: ignore[union-attr]
        self.assertEqual(load_profile("news.bbc.com", self.dir).name, "BBC News")  # type: ignore[union-attr]
        self.assertEqual(load_profile("m.news.bbc.com", self.dir).name, "BBC News")  # type: ignore[union-attr]

    def test_unknown_domain_returns_none(self) -> None:
        save_profile({"domain": "bbc.com"}, self.dir)
        self.assertIsNone(load_profile("cnn.com", self.dir))
        self.assertIsNone(load_profile("", self.dir))
        self.assertIsNone(load_profile("bbc.com", self.dir / "missing-dir"))

    def test_bad_files_warn_and_are_ignored(self) -> None:
        self._write("broken.com.json", "{not json")
        self._write("invalid.com.json", {"domain": "invalid.com", "article": {"body": 1}})
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            self.assertIsNone(load_profile("broken.com", self.dir))
            self.assertIsNone(load_profile("invalid.com", self.dir))
        messages = [str(item.message) for item in captured]
        self.assertTrue(any("broken.com.json" in message for message in messages))
        self.assertTrue(any("invalid.com.json" in message for message in messages))

    def test_template_file_not_treated_as_profile(self) -> None:
        self._write("_template.json", {"domain": "example.com", "name": "Template"})
        self.assertIsNone(load_profile("example.com", self.dir))

    def test_list_profiles(self) -> None:
        save_profile({"domain": "bbc.com", "name": "BBC", "language": "en"}, self.dir)
        save_profile({"domain": "lemonde.fr", "name": "Le Monde", "language": "fr"}, self.dir)
        entries = list_profiles(self.dir)
        self.assertEqual(len(entries), 2)
        domains = {entry["domain"] for entry in entries}
        self.assertEqual(domains, {"bbc.com", "lemonde.fr"})
        for entry in entries:
            self.assertEqual(
                set(entry),
                {"domain", "name", "language", "path", "learned_by", "updated_at"},
            )
            self.assertIsInstance(entry["path"], str)
        self.assertEqual(list_profiles(self.dir / "missing"), [])

    def test_load_profile_for_url(self) -> None:
        save_profile({"domain": "bbc.com", "name": "BBC"}, self.dir)
        self.assertIsNotNone(
            load_profile_for_url("https://www.bbc.com/news/articles/abc", self.dir)
        )
        self.assertIsNone(load_profile_for_url("https://cnn.com/news", self.dir))


class TemplateTest(unittest.TestCase):
    def test_template_contains_required_groups(self) -> None:
        template = profile_template()
        self.assertIsInstance(template, dict)
        for key in ("domain", "list", "article", "fetch", "discovery"):
            self.assertIn(key, template)
        self.assertIn("article p", template["article"]["body"])
        self.assertIn("::attr(href)", template["list"]["item"])

    def test_template_is_valid_profile(self) -> None:
        profile = validate_profile(profile_template())
        self.assertIsInstance(profile, SiteProfile)
        self.assertEqual(profile.domain, "example.com")


if __name__ == "__main__":
    unittest.main()
