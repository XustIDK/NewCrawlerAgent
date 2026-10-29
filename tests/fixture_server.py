# -*- coding: utf-8 -*-
"""离线 fixture HTTP 服务：供 test_agent_offline.py 等确定性 E2E 测试使用。

在 127.0.0.1 的随机端口上把 fixture HTML/XML 作为静态新闻站点返回，
让被测流水线走真实 HTTP 请求路径，但不访问外网。

路由值支持：
- fixture 名称（manifest 中存在）  -> 返回该 fixture 原始字节；
- str / bytes                      -> 直接作为原始响应体（测试可动态改写链接）；
- (status, str|bytes) 元组          -> 自定义状态码；
启动后仍可修改 server.routes，方便测试根据随机端口改写 HTML。
"""

from __future__ import annotations

import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from tests.fixtures import loader

ROBOTS_TXT = "User-agent: *\nAllow: /\n"
RouteValue = str | bytes | tuple[int, str | bytes] | tuple[str, str, int]


class _FixtureRequestHandler(BaseHTTPRequestHandler):
    server_version = "FixtureNewsServer/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        site: FixtureSiteServer = self.server.fixture_site  # type: ignore[attr-defined]
        path = urlsplit(self.path).path
        site.requests.append(self.path)
        status, body, content_type, extra_headers = site.resolve(path)
        self._send(status, body, content_type, extra_headers)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def _send(
        self, status: int, body: bytes, content_type: str, extra_headers: dict[str, str] | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """静默请求日志，避免测试输出噪声。"""


class FixtureSiteServer:
    """本地 fixture 新闻站点（上下文管理器可用）。"""

    def __init__(self, routes: dict[str, RouteValue] | None = None) -> None:
        self.routes: dict[str, RouteValue] = dict(routes or {})
        self.requests: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- 生命周期 ----------------------------------------------------------

    def start(self) -> "FixtureSiteServer":
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureRequestHandler)
        server.daemon_threads = True
        server.fixture_site = self  # type: ignore[attr-defined]
        self._server = server
        thread = threading.Thread(target=server.serve_forever, name="fixture-http", daemon=True)
        thread.start()
        self._thread = thread
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "FixtureSiteServer":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- 路由 --------------------------------------------------------------

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("FixtureSiteServer 尚未 start()")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    def add(self, path: str, value: RouteValue) -> None:
        if not path.startswith("/"):
            path = "/" + path
        self.routes[path] = value

    def request_paths(self) -> list[str]:
        return [urlsplit(item).path for item in self.requests]

    def add_redirect(self, path: str, location: str, status: int = 302) -> None:
        """注册一个跳转路由（用于测试“候选重定向到首页”等场景）。"""
        self.add(path, ("redirect", location, status))  # type: ignore[arg-type]

    def resolve(self, path: str) -> tuple[int, bytes, str, dict[str, str]]:
        if path == "/robots.txt":
            return 200, ROBOTS_TXT.encode("utf-8"), "text/plain; charset=utf-8", {}
        value: Any = self.routes.get(path)
        if value is None:
            return 404, b"not found", "text/plain; charset=utf-8", {}
        if isinstance(value, tuple) and len(value) == 3 and value[0] == "redirect":
            location = str(value[1])
            status = int(value[2]) if value[2] else 302
            return status, b"", "text/plain; charset=utf-8", {"Location": location}
        status = 200
        if isinstance(value, tuple):
            status, value = value
        try:
            body = self._as_bytes(value)
        except loader.FixtureError:
            return 500, b"fixture missing", "text/plain; charset=utf-8", {}
        return status, body, _content_type(body), {}

    @staticmethod
    def _as_bytes(value: Any) -> bytes:
        if isinstance(value, bytes):
            return value
        text = str(value)
        if text in loader.fixture_names():
            return loader.load_fixture(text).bytes
        if text.lstrip().startswith(("<", "{", "[")):
            return text.encode("utf-8")
        raise loader.FixtureError(f"未知路由内容：{text[:60]!r}")


def _content_type(body: bytes) -> str:
    sample = body[:2000].lstrip()
    if sample.startswith(b"<?xml") or sample.startswith(b"<rss") or sample.startswith(b"<feed"):
        return "application/rss+xml; charset=utf-8"
    if sample.startswith(b"{") or sample.startswith(b"["):
        return "application/json; charset=utf-8"
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        # 非 UTF-8 页面（如 GB18030）：不声明 charset，让抓取层自行识别编码。
        return "text/html"
    return "text/html; charset=utf-8"


def rewrite_host(html: str, old_host: str, new_base: str) -> str:
    """把 HTML 中的绝对链接从线上站点域名改写成本地 fixture 服务地址。"""
    return html.replace(old_host.rstrip("/"), new_base.rstrip("/"))


def extract_local_links(html: str, base_url: str) -> list[str]:
    """从（已改写域名的）HTML 中提取本地服务路径，按出现顺序去重。"""
    pattern = re.compile(re.escape(base_url.rstrip("/")) + r"(/[^\s\"'<>]*)")
    seen: set[str] = set()
    paths: list[str] = []
    for match in pattern.finditer(html):
        path = urlsplit(match.group(1)).path or "/"
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths
