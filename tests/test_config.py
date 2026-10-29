# -*- coding: utf-8 -*-
"""news_agent.config 单元测试：settings 合并、.env 加载、环境变量优先级、CrawlConfig 工厂。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from news_agent.config import (
    DEFAULT_SETTINGS,
    build_config,
    deep_merge,
    get_llm_config,
    load_local_env,
    load_settings,
    resolve_output_dir,
)
from news_agent.models import CrawlConfig


class SettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str, content: str) -> Path:
        path = self.dir / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_missing_file_uses_defaults(self) -> None:
        settings = load_settings(self.dir / "missing.json")
        self.assertEqual(settings["crawl"]["max_articles"], 10)
        self.assertEqual(settings["llm"]["max_rounds"], 12)
        self.assertEqual(settings["output"]["dir"], "data")

    def test_partial_config_is_deep_merged(self) -> None:
        path = self._write(
            "settings.json",
            json.dumps({"crawl": {"max_articles": 3, "timeout_s": 5}, "custom": {"a": 1}}),
        )
        settings = load_settings(path)
        self.assertEqual(settings["crawl"]["max_articles"], 3)
        self.assertEqual(settings["crawl"]["timeout_s"], 5)
        self.assertEqual(settings["crawl"]["retries"], 2)
        self.assertEqual(settings["custom"], {"a": 1})
        self.assertEqual(settings["llm"]["model"], DEFAULT_SETTINGS["llm"]["model"])

    def test_invalid_json_uses_defaults(self) -> None:
        path = self._write("bad.json", "{not json")
        self.assertEqual(load_settings(path), DEFAULT_SETTINGS)

    def test_non_dict_json_uses_defaults(self) -> None:
        for content in ("[]", '"text"', "123", "null"):
            with self.subTest(content=content):
                path = self._write("nondict.json", content)
                self.assertEqual(load_settings(path), DEFAULT_SETTINGS)

    def test_default_settings_are_not_mutated(self) -> None:
        settings = load_settings(self.dir / "missing.json")
        settings["crawl"]["max_articles"] = 999
        settings["crawl"]["include_patterns"].append("x")
        self.assertEqual(DEFAULT_SETTINGS["crawl"]["max_articles"], 10)
        self.assertEqual(DEFAULT_SETTINGS["crawl"]["include_patterns"], [])

    def test_repository_settings_file_loads(self) -> None:
        settings = load_settings()
        self.assertIsInstance(settings.get("crawl"), dict)
        self.assertGreaterEqual(settings["crawl"]["max_articles"], 1)


class LocalEnvTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, content: str) -> Path:
        path = self.dir / ".env"
        path.write_text(content, encoding="utf-8")
        return path

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(load_local_env(self.dir / "none.env"), {})

    def test_parses_comments_quotes_and_ignores_invalid_lines(self) -> None:
        path = self._write(
            "\n".join(
                [
                    "# 注释行",
                    "",
                    "NEWS_AGENT_A=plain",
                    'NEWS_AGENT_B="hello world"',
                    "NEWS_AGENT_C='single quoted'",
                    "NEWS_AGENT_D=",
                    "NO_EQUALS_LINE",
                    "  NEWS_AGENT_E = spaced value  ",
                ]
            )
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            loaded = load_local_env(path)
            self.assertEqual(loaded["NEWS_AGENT_A"], "plain")
            self.assertEqual(loaded["NEWS_AGENT_B"], "hello world")
            self.assertEqual(loaded["NEWS_AGENT_C"], "single quoted")
            self.assertEqual(loaded["NEWS_AGENT_D"], "")
            self.assertEqual(loaded["NEWS_AGENT_E"], "spaced value")
            self.assertNotIn("NO_EQUALS_LINE", loaded)
            self.assertEqual(os.environ["NEWS_AGENT_A"], "plain")

    def test_existing_system_environment_wins(self) -> None:
        path = self._write("NEWS_AGENT_KEY=from-file\n")
        with mock.patch.dict(os.environ, {"NEWS_AGENT_KEY": "from-system"}, clear=False):
            loaded = load_local_env(path)
            self.assertEqual(loaded["NEWS_AGENT_KEY"], "from-file")
            self.assertEqual(os.environ["NEWS_AGENT_KEY"], "from-system")


class LlmConfigTest(unittest.TestCase):
    def test_environment_overrides_key_and_model(self) -> None:
        settings = {
            "llm": {
                "base_url": "https://api.example.com",
                "model": "deepseek-chat",
                "max_rounds": "7",
                "temperature": "0.5",
            }
        }
        with mock.patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": " key123 ", "DEEPSEEK_MODEL": "deepseek-reasoner"},
            clear=False,
        ):
            config = get_llm_config(settings)
        self.assertEqual(config["api_key"], "key123")
        self.assertEqual(config["base_url"], "https://api.example.com")
        self.assertEqual(config["model"], "deepseek-reasoner")
        self.assertEqual(config["max_rounds"], 7)
        self.assertIsInstance(config["max_rounds"], int)
        self.assertEqual(config["temperature"], 0.5)
        self.assertIsInstance(config["temperature"], float)

    def test_defaults_without_environment(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEEPSEEK_API_KEY", None)
            os.environ.pop("DEEPSEEK_MODEL", None)
            config = get_llm_config({"llm": {}})
        self.assertEqual(config["api_key"], "")
        self.assertEqual(config["base_url"], "https://api.deepseek.com")
        self.assertEqual(config["model"], "deepseek-flash")
        self.assertEqual(config["max_rounds"], 12)
        self.assertEqual(config["temperature"], 0.2)


class DeepMergeTest(unittest.TestCase):
    def test_nested_merge_and_no_mutation(self) -> None:
        base = {"a": {"b": 1, "c": [1]}, "d": 2}
        override = {"a": {"b": 3, "e": 4}, "d": 5}
        merged = deep_merge(base, override)
        self.assertEqual(merged, {"a": {"b": 3, "c": [1], "e": 4}, "d": 5})
        self.assertEqual(base, {"a": {"b": 1, "c": [1]}, "d": 2})
        merged["a"]["c"].append(2)
        self.assertEqual(base["a"]["c"], [1])


class BuildConfigTest(unittest.TestCase):
    def test_builds_crawl_config_from_settings_and_overrides(self) -> None:
        settings = {
            "crawl": {"max_articles": 6, "timeout_s": 5},
            "llm": {"max_rounds": 9},
            "user_agent": "Test-UA",
            "output": {"dir": "custom_out"},
        }
        config = build_config("example.com/news", settings, max_articles=2)
        self.assertIsInstance(config, CrawlConfig)
        self.assertEqual(config.entry_url, "https://example.com/news")
        self.assertEqual(config.domain, "example.com")
        self.assertEqual(config.max_articles, 2)
        self.assertEqual(config.timeout_s, 5)
        self.assertEqual(config.max_rounds, 9)
        self.assertEqual(config.user_agent, "Test-UA")
        self.assertEqual(config.out_dir, "custom_out")

    def test_uses_repository_settings_when_none(self) -> None:
        config = build_config("https://example.com")
        self.assertGreaterEqual(config.max_articles, 1)
        self.assertGreaterEqual(config.max_rounds, 1)


class ResolveOutputDirTest(unittest.TestCase):
    def test_absolute_settings_dir_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out1"
            result = resolve_output_dir({"output": {"dir": str(target)}})
            self.assertEqual(result, target)
            self.assertTrue(result.is_dir())

    def test_out_dir_parameter_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = {"output": {"dir": str(Path(tmp) / "settings_out")}}
            target = Path(tmp) / "explicit_out"
            result = resolve_output_dir(settings, out_dir=str(target))
            self.assertEqual(result, target)
            self.assertTrue(result.is_dir())

    def test_relative_dir_resolved_against_base_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("news_agent.config.BASE_DIR", Path(tmp)):
                result = resolve_output_dir({})
            self.assertEqual(result, Path(tmp) / "data")
            self.assertTrue(result.is_dir())


if __name__ == "__main__":
    unittest.main()
