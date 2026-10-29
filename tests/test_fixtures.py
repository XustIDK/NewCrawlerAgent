# -*- coding: utf-8 -*-
"""tests/fixtures 完整性与 loader 单元测试（不联网）。

校验 manifest 字段、sha256/字节数、站点覆盖、fixture 可解析与编码正确，
避免采集脚本或文件损坏导致后续模块测试“假绿”。
"""

from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime

from news_agent import dom

from tests.fixtures import loader

REQUIRED_FIELDS = {
    "name",
    "path",
    "source_url",
    "encoding_used",
    "bytes",
    "sha256",
    "fetched_at",
    "kind",
    "notes",
}
MAX_TOTAL_BYTES = 8 * 1024 * 1024


class ManifestIntegrityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = loader.load_manifest()
        cls.entries = cls.manifest["fixtures"]

    def test_manifest_version_and_total_bytes(self) -> None:
        self.assertEqual(self.manifest["version"], 1)
        self.assertEqual(self.manifest["total_bytes"], sum(entry["bytes"] for entry in self.entries))
        self.assertLess(self.manifest["total_bytes"], MAX_TOTAL_BYTES)

    def test_required_fields_and_unique_names(self) -> None:
        names = [entry["name"] for entry in self.entries]
        self.assertEqual(len(names), len(set(names)))
        for entry in self.entries:
            with self.subTest(name=entry.get("name")):
                self.assertTrue(REQUIRED_FIELDS <= set(entry))
                self.assertIn(entry["kind"], ("list", "article", "feed"))
                self.assertTrue(entry["notes"])
                datetime.fromisoformat(entry["fetched_at"])

    def test_file_size_sha256_and_encoding_match(self) -> None:
        for entry in self.entries:
            with self.subTest(name=entry["name"]):
                path = loader.FIXTURES_DIR / entry["path"]
                self.assertTrue(path.is_file())
                content = path.read_bytes()
                self.assertEqual(len(content), entry["bytes"])
                self.assertEqual(hashlib.sha256(content).hexdigest(), entry["sha256"])
                text, encoding = loader.decode_bytes(content, entry["encoding_used"])
                self.assertEqual(encoding, entry["encoding_used"])
                self.assertNotIn("\ufffd", text[:5000])

    def test_expected_sites_are_covered(self) -> None:
        sites = {entry["path"].split("/")[0] for entry in self.entries}
        self.assertTrue({"bbc", "dw", "nhk", "lemonde", "sina", "zaobao"} <= sites)
        kinds = {entry["name"]: entry["kind"] for entry in self.entries}
        self.assertEqual(kinds["bbc_rss"], "feed")
        self.assertEqual(kinds["bbc_news_list"], "list")
        self.assertGreaterEqual(
            sum(1 for entry in self.entries if entry["kind"] == "article"), 7
        )

    def test_list_and_article_fixtures_are_parsable_html(self) -> None:
        for entry in self.entries:
            if entry["kind"] == "feed":
                continue
            with self.subTest(name=entry["name"]):
                fixture = loader.load_fixture(entry["name"])
                tree = dom.parse_html(fixture.text)
                self.assertEqual(tree.tag, "html")
                self.assertTrue(dom.select_nodes(tree, "body") or dom.select_nodes(tree, "head"))
                self.assertGreater(len(fixture.text), 1000)


class LoaderTest(unittest.TestCase):
    def test_load_fixture_returns_text_bytes_and_metadata(self) -> None:
        fixture = loader.load_fixture("bbc_rss")
        self.assertEqual(fixture.kind, "feed")
        self.assertEqual(fixture.name, "bbc_rss")
        self.assertTrue(fixture.url.startswith("https://feeds.bbci.co.uk/"))
        self.assertEqual(fixture.bytes, fixture.data)
        self.assertEqual(fixture.size, len(fixture.data))
        self.assertEqual(fixture.path, loader.FIXTURES_DIR / "bbc" / "bbc_rss.html")
        self.assertTrue(fixture.text.lstrip().startswith("<?xml"))
        self.assertIn("<rss", fixture.text)

    def test_fixture_names_and_kind_filter(self) -> None:
        names = loader.fixture_names()
        self.assertIn("bbc_news_list", names)
        self.assertIn("bbc_rss", loader.fixture_names("feed"))
        self.assertNotIn("bbc_news_list", loader.fixture_names("feed"))

    def test_list_fixtures_returns_metadata_only(self) -> None:
        entries = loader.list_fixtures()
        self.assertEqual(len(entries), len(loader.fixture_names()))
        self.assertIn("path", entries[0])
        self.assertNotIn("text", entries[0])

    def test_unknown_fixture_raises_fixture_error(self) -> None:
        with self.assertRaises(loader.FixtureError):
            loader.load_fixture("no_such_fixture")

    def test_decode_bytes_prefers_recorded_encoding_and_falls_back(self) -> None:
        text, encoding = loader.decode_bytes("中文内容".encode("gb18030"), "gb18030")
        self.assertEqual(text, "中文内容")
        self.assertEqual(encoding, "gb18030")
        text, encoding = loader.decode_bytes(b"hello", "utf-8")
        self.assertEqual((text, encoding), ("hello", "utf-8"))
        text, encoding = loader.decode_bytes("中文".encode("gb18030"), "")
        self.assertEqual(text, "中文")
        self.assertEqual(encoding, "gb18030")

    def test_manifest_file_missing_raises(self) -> None:
        with self.assertRaises(loader.FixtureError):
            loader.load_manifest(loader.FIXTURES_DIR / "missing-manifest.json")


class EncodingFixtureTest(unittest.TestCase):
    def test_synthetic_gbk_fixture_decodes_without_mojibake(self) -> None:
        names = loader.fixture_names("article")
        if "sina_article_gbk" not in names:
            self.skipTest("synthetic GBK fixture not collected")
        fixture = loader.load_fixture("sina_article_gbk")
        self.assertEqual(fixture.encoding, "gb18030")
        self.assertNotIn("\ufffd", fixture.text[:20000])
        chinese_chars = [char for char in fixture.text if "\u4e00" <= char <= "\u9fff"]
        self.assertGreater(len(chinese_chars), 100)
        self.assertIn("charset", fixture.text[:4096].lower())

    def test_utf8_chinese_fixture_has_no_replacement_chars(self) -> None:
        fixture = loader.load_fixture("sina_article_1")
        self.assertNotIn("\ufffd", fixture.text)
        chinese_chars = [char for char in fixture.text if "\u4e00" <= char <= "\u9fff"]
        self.assertGreater(len(chinese_chars), 100)


if __name__ == "__main__":
    unittest.main()
