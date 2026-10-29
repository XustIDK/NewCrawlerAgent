# -*- coding: utf-8 -*-
"""确定性流水线离线 E2E：本地 fixture HTTP 服务 + run_deterministic（不联网/不用 API Key）。

覆盖：
- BBC 列表页 fixture（改写为本地链接）→ 发现 → 抓取 2 篇 BBC 文章 fixture → 输出数据集；
- GB18030 编码文章页经本地 HTTP 返回 → 抓取层编码识别 → 中文抽取无乱码；
- 入口 404 时流水线不抛异常、仍写兜底产物；
- 全程由 socket 护栏保证不访问外网。
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest import mock
from urllib.parse import urlsplit

from news_agent import dom
from news_agent.agent import run_deterministic
from news_agent.config import build_config
from news_agent.urls import absolute_url
from tests.fixture_server import FixtureSiteServer, rewrite_host
from tests.fixtures.loader import load_fixture

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"}


@contextmanager
def offline_network_guard() -> Iterator[None]:
    """禁止测试期间任何非本机网络连接/DNS 解析（防止 E2E 意外联网）。"""
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self: socket.socket, address: object, *args: object, **kwargs: object) -> object:
        host = address[0] if isinstance(address, (tuple, list)) and address else address
        if str(host) not in _ALLOWED_HOSTS:
            raise OSError(101, f"offline test forbids network access to {host!r}")
        return real_connect(self, address, *args, **kwargs)  # type: ignore[arg-type]

    def guarded_getaddrinfo(host: object, port: object, *args: object, **kwargs: object) -> object:
        if host is not None and str(host) not in _ALLOWED_HOSTS:
            raise socket.gaierror(f"offline test forbids DNS lookup of {host!r}")
        return real_getaddrinfo(host, port, *args, **kwargs)  # type: ignore[arg-type]

    with mock.patch.object(socket.socket, "connect", guarded_connect), mock.patch.object(
        socket, "getaddrinfo", guarded_getaddrinfo
    ):
        yield


def local_paths_for(html: str, page_url: str, base_url: str, suffix: str = "") -> list[str]:
    """把 HTML 中会解析到本地服务的链接路径列出去重（用于把文章页映射到 fixture）。"""
    tree = dom.parse_html(html)
    paths: list[str] = []
    for href in dom.select_values(tree, "a::attr(href)"):
        target = absolute_url(page_url, href)
        if not target or not target.startswith(base_url):
            continue
        path = urlsplit(target).path
        if suffix and not path.endswith(suffix):
            continue
        if path not in paths:
            paths.append(path)
    return paths


class OfflinePipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.server = FixtureSiteServer({}).start()
        base = cls.server.base_url

        # BBC 列表页 fixture：article 链接多为相对路径，按本地页面 URL 解析后映射文章 fixture。
        bbc_list = load_fixture("bbc_news_list").text
        bbc_page_url = cls.server.url("/news/")
        cls.server.add("/news/", bbc_list)
        cls.server.add("/news", bbc_list)
        article_paths = [
            path for path in local_paths_for(bbc_list, bbc_page_url, base) if "/news/articles/" in path
        ]
        if not article_paths:  # pragma: no cover - fixture 结构变化时的防御
            raise unittest.SkipTest("BBC fixture 中未找到可映射的文章链接")
        for index, path in enumerate(article_paths):
            cls.server.add(path, "bbc_article_1" if index % 2 == 0 else "bbc_article_2")
        cls.bbc_article_paths = article_paths

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self._env = mock.patch.dict(
            os.environ,
            {"NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1"},
            clear=False,
        )
        self._env.start()
        os.environ.pop("DEEPSEEK_API_KEY", None)

    def tearDown(self) -> None:
        self._env.stop()

    def build(self, entry_url: str, out_name: str = "out", **overrides: object):
        settings = {
            "crawl": {
                "max_articles": 2,
                "max_list_pages": 1,
                "min_delay_s": 0.0,
                "browser_mode": "never",
                "respect_robots": True,
                "timeout_s": 10,
                "retries": 0,
            },
            "output": {"dir": str(self.root / out_name)},
        }
        config = build_config(
            entry_url,
            settings,
            max_articles=2,
            max_list_pages=1,
            use_llm=False,
            browser_mode="never",
            min_delay_s=0.0,
            timeout_s=10,
            retries=0,
            out_dir=str(self.root / out_name),
            **overrides,  # type: ignore[arg-type]
        )
        return config, settings

    def test_deterministic_pipeline_end_to_end(self) -> None:
        entry = self.server.url("/news/")
        config, settings = self.build(entry)
        events: list[tuple[str, str]] = []
        with offline_network_guard():
            result = run_deterministic(
                config, settings, on_event=lambda message, phase=None: events.append((phase or "", message))
            )
        self.assertEqual(len(result.articles), 2)
        for article in result.articles:
            self.assertTrue(article.title)
            self.assertGreater(len(article.body), 1000)
            self.assertRegex(article.published_at, r"^\d{4}-\d{2}-\d{2}T")
            self.assertEqual(article.language, "en")
            self.assertTrue(article.url.startswith(self.server.base_url))
            self.assertTrue(article.list_page.startswith(self.server.base_url))
        self.assertIn(result.discovery_method, ("heuristic", "mixed"))
        self.assertGreaterEqual(result.stats.candidates, 5)
        self.assertEqual(result.stats.errors, [])
        # 产物：JSONL / CSV / run_meta / report / SVG（PNG 可选）
        out_dir = Path(result.output_files["dir"])
        self.assertTrue(out_dir.is_dir())
        self.assertTrue(out_dir.is_relative_to(self.root))
        for name in ("articles.jsonl", "articles.csv", "run_meta.json", "report.md", "agent_loop.svg"):
            self.assertTrue((out_dir / name).is_file(), name)
        rows = [
            json.loads(line)
            for line in (out_dir / "articles.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(rows), 2)
        self.assertIn("id", rows[0])
        self.assertIn("title", rows[0])
        meta = json.loads((out_dir / "run_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["article_count"], 2)
        self.assertEqual(meta["entry_url"], entry)
        report = (out_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("新闻抓取报告", report)
        self.assertIn(entry, report)
        # 进度事件覆盖关键阶段
        phases = {phase for phase, _ in events}
        self.assertTrue({"starting", "discovering", "extracting", "saving"} <= phases)

    def test_no_external_network_and_no_api_key(self) -> None:
        entry = self.server.url("/news/")
        config, settings = self.build(entry, out_name="out_no_key")
        with offline_network_guard():
            result = run_deterministic(config, settings)
        self.assertEqual(len(result.articles), 2)
        self.assertEqual(os.environ.get("DEEPSEEK_API_KEY"), None)
        # 所有抓取请求都落在本地 fixture 服务
        for path in self.server.request_paths():
            self.assertTrue(path.startswith("/"))

    def test_gb18030_article_page_decoded_without_mojibake(self) -> None:
        """本地返回 GB18030 编码文章页 → 抓取层识别编码 → 中文标题/正文无乱码。"""
        list_html = """<html><head><meta charset="utf-8"><title>中文新闻列表</title></head>
        <body><h1>中文新闻列表</h1><div class="list">
        <div class="item"><h2><a href="/gbk/2026-10-29/doc-article-1.shtml">红军铁纪篇：打破谣言的中文新闻标题</a></h2></div>
        <div class="item"><h2><a href="/gbk/2026-10-29/doc-article-2.shtml">第二条中文新闻标题用于发现测试</a></h2></div>
        </div></body></html>"""
        base = self.server.base_url
        self.server.add("/gbk/", list_html)
        for path in ("/gbk/2026-10-29/doc-article-1.shtml", "/gbk/2026-10-29/doc-article-2.shtml"):
            self.server.add(path, load_fixture("sina_article_gbk").bytes)
        config, settings = self.build(self.server.url("/gbk/"), out_name="out_gbk")
        with offline_network_guard():
            result = run_deterministic(config, settings)
        self.assertGreaterEqual(len(result.articles), 1)
        article = result.articles[0]
        self.assertIn("红军", article.title)
        self.assertFalse("\ufffd" in article.title or "\ufffd" in article.body)
        self.assertEqual(article.language, "zh")
        self.assertGreater(len(article.body), 200)
        self.assertTrue(article.published_at.startswith("2026-10-29"))

    def test_redirect_to_homepage_candidate_is_skipped(self) -> None:
        """候选 302 到站点首页（新浪 RSS redirect.php 同款）时跳过，不产出"首页文章"。"""
        base = self.server.base_url
        list_html = """<html><head><meta charset="utf-8"><title>Redirect test list</title></head>
        <body><div class="list">
        <div class="item"><h2><a href="/news/2026/10/29/real-1">真实文章标题，包含足够长度的文本用于发现</a></h2></div>
        <div class="item"><h2><a href="/news/2026/10/29/moved">跳转候选标题，包含足够长度的文本用于发现</a></h2></div>
        </div></body></html>"""
        homepage = """<html><head><meta charset="utf-8"><title>Example Home</title></head>
        <body><h1>Example Home</h1><nav>首页 导航 内容 链接 菜单</nav></body></html>"""
        self.server.add("/redirect-test/", list_html)
        self.server.add("/news/2026/10/29/real-1", "bbc_article_1")
        self.server.add("/news/2026/10/29/moved", ("redirect", "/", 302))
        self.server.add("/", homepage)
        config, settings = self.build(self.server.url("/redirect-test/"), out_name="out_redirect")
        with offline_network_guard():
            result = run_deterministic(config, settings)
        self.assertEqual(len(result.articles), 1)
        article = result.articles[0]
        self.assertEqual(article.url, base + "/news/2026/10/29/real-1")
        self.assertNotEqual(article.title, "Example Home")
        self.assertFalse(any(item.url == base + "/" for item in result.articles))
        self.assertTrue(
            any("重定向到非文章页" in warning for warning in result.stats.warnings),
            result.stats.warnings,
        )
        self.assertEqual(result.stats.errors, [])
        # Agent 预抓取 + 抽取共用 Fetcher 运行内缓存：真实文章只请求 1 次，跳转候选只请求 1 次。
        request_paths = self.server.request_paths()
        self.assertEqual(request_paths.count("/news/2026/10/29/real-1"), 1)
        self.assertLessEqual(request_paths.count("/news/2026/10/29/moved"), 2)

    def test_entry_failure_writes_fallback_outputs(self) -> None:
        config, settings = self.build(self.server.url("/missing/"), out_name="out_failure")
        with offline_network_guard():
            result = run_deterministic(config, settings)
        self.assertEqual(result.articles, [])
        self.assertTrue(any("抓取失败" in warning for warning in result.stats.warnings))
        out_dir = Path(result.output_files["dir"])
        self.assertTrue((out_dir / "run_meta.json").is_file())
        self.assertTrue((out_dir / "report.md").is_file())
        self.assertEqual(
            json.loads((out_dir / "run_meta.json").read_text(encoding="utf-8"))["article_count"], 0
        )


if __name__ == "__main__":
    unittest.main()
