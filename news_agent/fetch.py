"""HTTP 抓取层：requests 静态抓取 + 编码识别 + robots / 限速 / 重试 + Playwright 浏览器回退。

对应 DESIGN.md §4.2 / §9.1 的契约：

- 静态优先：requests.Session，浏览器 UA，自动重定向，超时 config.timeout_s，
  指数退避重试 config.retries 次（带 jitter）；
- 编码识别：BOM → HTML meta charset → HTTP header charset（低置信度编码延后尝试）
  → UTF-8 严格解码 → apparent_encoding → latin-1 兜底；站点画像可强制编码；
- 礼貌抓取：同 host 两次请求最小间隔 config.min_delay_s；读取并遵守 robots.txt；
- 浏览器回退：browser_mode=auto 时，若静态结果像 JS 空壳（可见文本过少 /
  HTML 很短 / 命中 enable javascript 特征）则改用 Playwright 打开 Chrome
  （channel=chrome，失败降级默认 chromium，headless 可配置）；
- 人工介入：可见模式下疑似验证码 / Cloudflare 挑战时通过 on_event 通知，
  并等待 intervention_event（最多 180 秒），处理完继续读取页面；
- 错误语义：网络 / HTTP 失败返回 ok=False 的 FetchResult（不抛异常）；
  robots.txt 拒绝抛 RobotsDenied；明显非法 URL 抛 FetchError。
"""

from __future__ import annotations

import codecs
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

from .errors import FetchError, RobotsDenied
from .models import CrawlConfig
from .urls import is_http_url, site_root

__all__ = [
    "FetchResult",
    "Fetcher",
    "detect_encoding",
    "decode_content",
    "BROWSER_ACCEPT",
    "BROWSER_ACCEPT_LANGUAGE",
]

# 浏览器导航头：新闻站普遍按 Accept-Language 返回不同语言版本，这里给多语言优先级。
BROWSER_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "text/plain;q=0.8,*/*;q=0.7"
)
BROWSER_ACCEPT_LANGUAGE = "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7,ja;q=0.6,fr;q=0.5,de;q=0.5,es;q=0.4"

# 需要指数退避重试的 HTTP 状态码（网络层异常另行判断）。
_RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})

# 需要“换浏览器再试一次”的 HTTP 状态码：
# - 402 常见于付费墙/反爬（如 Le Monde 文章页），浏览器可能可读；
# - 401/403/406/429 为认证/反爬/限流语义；
# - 503 是 Cloudflare 常用的挑战页状态。
# 404/410 等“确定性不存在”不重试，避免白白启动浏览器。
_BROWSER_RETRY_STATUS = frozenset({401, 402, 403, 406, 429, 503})

def _canonical_names(*names: str) -> frozenset[str]:
    """把编码别名集合转成 codecs 规范名集合（如 iso-8859-1 -> iso8859-1）。"""
    resolved: set[str] = set()
    for name in names:
        try:
            resolved.add(codecs.lookup(name).name)
        except (LookupError, ValueError):
            resolved.add(name)
    return frozenset(resolved)


# 低置信度编码：很多服务器无条件返回 ISO-8859-1 / ASCII，真实编码通常是 UTF-8 或 GBK，
# 因此先忽略，等 UTF-8 严格解码与 apparent_encoding 都失败后再兜底。
_LOW_CONFIDENCE = _canonical_names(
    "iso-8859-1", "iso-8859-15", "ascii", "cp1252", "mac-roman"
)

# 传统 CJK 编码：用于判断“声明的编码”是否可信（这类页面偶尔会被误标）。
_LEGACY_CJK = _canonical_names(
    "gbk", "gb18030", "big5", "big5hkscs", "shift_jis", "euc_jp", "euc_kr", "cp949"
)

_ENCODING_ALIASES = {
    "gb2312": "gbk",
    "gb_2312": "gbk",
    "gb-2312": "gbk",
    "gb_2312-80": "gbk",
    "x-gbk": "gbk",
    "gb18030": "gb18030",
    "utf8": "utf-8",
    "utf-8": "utf-8",
    "unicode-1-1-utf-8": "utf-8",
    "shift_jis": "shift-jis",
    "shiftjis": "shift-jis",
    "sjis": "shift-jis",
    "x-sjis": "shift-jis",
    "euc_kr": "euc-kr",
    "ks_c_5601-1987": "euc-kr",
    "big5-hkscs": "big5hkscs",
    "latin1": "iso-8859-1",
    "latin-1": "iso-8859-1",
    "windows-1252": "cp1252",
    "us-ascii": "ascii",
    "utf-16le": "utf-16",
    "utf-16be": "utf-16",
}

# 顺序很重要：UTF-32 LE 的 BOM 以 FF FE 开头，必须先于 UTF-16 LE 判断。
_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)

_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+?charset\s*=\s*["']?\s*([A-Za-z0-9][A-Za-z0-9_.:\-]*)""",
    re.IGNORECASE,
)
_META_HTTP_EQUIV_RE = re.compile(
    rb"""<meta[^>]+?content\s*=\s*["'][^"']*?charset\s*=\s*([A-Za-z0-9][A-Za-z0-9_.:\-]*)""",
    re.IGNORECASE,
)
_HEADER_CHARSET_RE = re.compile(r"""charset\s*=\s*["']?([^\s;"']+)""", re.IGNORECASE)
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_JS_SHELL_MARKERS = (
    "enable javascript",
    "requires javascript",
    "please enable js",
    "javascript is disabled",
    "turn on javascript",
    "javascript を有効",
    "javascriptを有効",
    "启用javascript",
    "请启用javascript",
    "开启javascript",
    "请开启javascript",
)
_CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "verify you are human",
    "checking your browser",
    "ddos protection",
    "cf-challenge",
    "access denied",
    "人机验证",
    "安全验证",
    "访问验证",
    "验证码",
    "cloudflare",
)
_CHALLENGE_SELECTORS = (
    "#challenge-form",
    "#challenge-running",
    ".cf-turnstile",
    ".g-recaptcha",
    "[class*=captcha]",
    "[id*=captcha]",
    "iframe[src*=captcha]",
    "iframe[src*=challenge]",
    "form[action*=challenge]",
)
_BINARY_CONTENT_TYPES = ("image/", "video/", "audio/", "application/pdf", "application/zip")


# ---------------------------------------------------------------------------
# 编码识别与解码
# ---------------------------------------------------------------------------


def _normalize_encoding(name: str) -> str:
    """编码名归一化：小写、别名映射、校验 codecs 是否支持；无效返回空串。"""
    value = str(name or "").strip().strip("\"'").strip().lower()
    if not value or value in ("auto", "none", "unknown", "null"):
        return ""
    value = value.replace("_", "-").strip()
    value = _ENCODING_ALIASES.get(value, value)
    try:
        info = codecs.lookup(value)
    except (LookupError, ValueError):
        return ""
    return info.name


def _meta_charset(head: bytes) -> str:
    """从 HTML 前 8KB 的 meta 标签中取 charset（同时兼容 http-equiv Content-Type）。"""
    match = _META_CHARSET_RE.search(head)
    if match:
        return match.group(1).decode("ascii", "ignore")
    match = _META_HTTP_EQUIV_RE.search(head)
    if match:
        return match.group(1).decode("ascii", "ignore")
    return ""


def _header_charset(content_type: str) -> str:
    match = _HEADER_CHARSET_RE.search(str(content_type or ""))
    return match.group(1).strip() if match else ""


def _try_decode(content: bytes, encoding: str) -> str | None:
    """严格解码，失败返回 None（不抛异常）。"""
    if not encoding:
        return None
    try:
        text = content.decode(encoding, errors="strict")
    except (UnicodeDecodeError, LookupError, ValueError):
        return None
    return text.lstrip("\ufeff")


def _looks_bad_text(text: str) -> bool:
    """解码结果是否明显是乱码（替换符 / 典型 mojibake 组合）。"""
    if not text:
        return True
    sample = text[:4000]
    if "\ufffd" in sample:
        return True
    bad_markers = ("锟斤拷", "ï»¿", "â€", "Ã©", "Ã¨", "Ã¤", "Ã¶", "Ã¼", "Ð\x9f", "Ñ\x81")
    return sum(sample.count(marker) for marker in bad_markers) >= 3


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    sample = text[:6000]
    cjk = sum(
        1
        for char in sample
        if "\u3040" <= char <= "\u30ff"  # 日文假名
        or "\u3400" <= char <= "\u4dbf"  # CJK 扩展 A
        or "\u4e00" <= char <= "\u9fff"  # CJK 基本区
        or "\uac00" <= char <= "\ud7af"  # 韩文
    )
    return cjk / max(1, len(sample))


def detect_encoding(
    content: bytes,
    content_type: str = "",
    forced: str = "",
    apparent: str | Callable[[], str] = "",
) -> str:
    """按契约顺序识别网页编码，返回可用的 codecs 规范名。

    参数：
        content      : 响应原始字节；
        content_type : HTTP Content-Type 头（可含 charset）；
        forced       : 站点画像强制编码（"auto" 或空表示自动）；
        apparent     : response.apparent_encoding 字符串或其惰性回调（避免长页面重复猜测）。
    """
    forced_name = _normalize_encoding(forced) if str(forced).strip().lower() != "auto" else ""
    if forced_name:
        return forced_name
    if not content:
        return "utf-8"

    # 1) BOM 最可靠
    for bom, encoding in _BOMS:
        if content.startswith(bom):
            return encoding

    head = content[:8192]
    meta_enc = _normalize_encoding(_meta_charset(head))
    header_enc = _normalize_encoding(_header_charset(content_type))

    # 2) meta charset / header charset（低置信度编码留到最后）
    primary: list[str] = []
    for encoding in (meta_enc, header_enc):
        if encoding and encoding not in _LOW_CONFIDENCE and encoding not in primary:
            primary.append(encoding)
    for encoding in primary:
        text = _try_decode(content, encoding)
        if text is None or _looks_bad_text(text):
            continue
        if encoding in _LEGACY_CJK:
            # 声明的 CJK 编码偶尔是错的：若 UTF-8 严格解码也能成立且中文更多，优先 UTF-8。
            utf8_text = _try_decode(content, "utf-8")
            if utf8_text is not None and not _looks_bad_text(utf8_text):
                if _cjk_ratio(utf8_text) > max(_cjk_ratio(text), 0.12):
                    return "utf-8"
        return encoding

    # 3) UTF-8 严格解码
    utf8_text = _try_decode(content, "utf-8")
    if utf8_text is not None and not _looks_bad_text(utf8_text):
        return "utf-8"

    # 4) apparent_encoding（charset_normalizer / chardet 的猜测）
    apparent_name = ""
    try:
        raw_apparent = apparent() if callable(apparent) else apparent
        apparent_name = _normalize_encoding(str(raw_apparent or ""))
    except Exception:  # noqa: BLE001 - 猜测失败不影响兜底
        apparent_name = ""
    if apparent_name and apparent_name not in primary:
        text = _try_decode(content, apparent_name)
        if text is not None and not _looks_bad_text(text):
            return apparent_name

    # 5) 低置信度的声明编码兜底
    for encoding in (meta_enc, header_enc):
        if encoding and _try_decode(content, encoding) is not None:
            return encoding
    if apparent_name and _try_decode(content, apparent_name) is not None:
        return apparent_name
    if utf8_text is not None:
        return "utf-8"
    return "latin-1"


def decode_content(content: bytes, encoding: str) -> str:
    """按给定编码解码文本，尽量不抛异常；去掉 BOM。"""
    if content is None:
        return ""
    name = _normalize_encoding(encoding) or "utf-8"
    for candidate in (name, "utf-8", "latin-1"):
        try:
            return content.decode(candidate, errors="replace").lstrip("\ufeff")
        except (UnicodeDecodeError, LookupError, ValueError):
            continue
    return content.decode("latin-1", errors="replace").lstrip("\ufeff")


def _visible_text(html: str) -> str:
    """粗略提取 HTML 可见文本，只用于判断页面是不是 JS 空壳。"""
    if not html:
        return ""
    cleaned = _COMMENT_RE.sub(" ", html)
    cleaned = _SCRIPT_STYLE_RE.sub(" ", cleaned)
    cleaned = _TAG_RE.sub(" ", cleaned)
    return " ".join(cleaned.split())


def _merge_errors(first: str, second: str) -> str:
    """合并两段错误说明，控制长度。"""
    parts = [part.strip() for part in (first or "", second or "") if part and part.strip()]
    return "；".join(part[:400] for part in parts)[:800]


def _elapsed_ms(started: float) -> int:
    return int(max(0.0, (time.monotonic() - started)) * 1000)


# ---------------------------------------------------------------------------
# FetchResult
# ---------------------------------------------------------------------------


@dataclass
class FetchResult:
    """一次抓取的统一结果；失败时 ok=False 且 error 说明原因，不抛异常。"""

    url: str = ""
    final_url: str = ""
    status: int = 0
    text: str = ""
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    encoding: str = ""
    method: str = "http"  # "http" | "browser"
    elapsed_ms: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        if self.error:
            return False
        return 200 <= int(self.status or 0) < 400 and bool((self.text or "").strip())

    @property
    def content_type(self) -> str:
        raw = self.headers.get("content-type") or self.headers.get("Content-Type") or ""
        return raw.split(";")[0].strip().lower()

    def to_dict(self) -> dict[str, Any]:
        """给 Agent / run_meta 用的摘要（不含正文，避免 JSON 膨胀）。"""
        return {
            "url": self.url,
            "final_url": self.final_url,
            "status": self.status,
            "method": self.method,
            "encoding": self.encoding,
            "elapsed_ms": self.elapsed_ms,
            "text_chars": len(self.text or ""),
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------


class Fetcher:
    """抓取器：静态优先 + 浏览器回退，自带限速 / robots / 重试 / 编码识别。"""

    def __init__(
        self,
        config: CrawlConfig,
        settings: dict | None = None,
        on_event: Callable[[str, str], None] | None = None,
        intervention_event: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.settings = settings or {}
        self.on_event = on_event
        self.intervention_event = intervention_event
        self.fetch_settings: dict[str, Any] = dict(self.settings.get("fetch") or {})

        self._session = requests.Session()
        self._session.max_redirects = 5
        self._session.trust_env = True
        self._session.headers.update(
            {
                "User-Agent": config.user_agent or "Mozilla/5.0",
                "Accept": BROWSER_ACCEPT,
                "Accept-Language": BROWSER_ACCEPT_LANGUAGE,
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
        )

        self._last_request: dict[str, float] = {}
        self._robots_cache: dict[str, RobotFileParser | None] = {}
        self._cache: dict[tuple[Any, ...], FetchResult] = {}
        self._robots_lock = threading.Lock()

        self._browser_lock = threading.RLock()
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._browser_error = ""
        self._closed = False

    # -- 事件 -------------------------------------------------------------

    def _emit(self, message: str, phase: str = "fetching") -> None:
        """进度事件；回调异常绝不能影响抓取主流程。"""
        callback = self.on_event
        if not callback:
            return
        try:
            callback(str(message), phase)
        except Exception:  # noqa: BLE001 - 回调是外部代码，失败忽略
            return

    # -- 基础工具 ---------------------------------------------------------

    def _prepare_url(self, url: str) -> str:
        value = str(url or "").strip()
        if not value or not is_http_url(value):
            raise FetchError(f"非法 URL：{url!r}")
        return value

    def _throttle(self, url: str) -> None:
        """同 host 两次请求之间保证 min_delay_s 间隔（time.monotonic 计时）。"""
        delay = float(self.config.min_delay_s or 0)
        if delay <= 0:
            return
        host = urlsplit(url).netloc.lower()
        if not host:
            return
        last = self._last_request.get(host)
        if last is not None:
            remaining = delay - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request[host] = time.monotonic()

    def _backoff_seconds(self, attempt: int) -> float:
        base = float(self.fetch_settings.get("backoff_s", 0.8) or 0.8)
        cap = float(self.fetch_settings.get("max_backoff_s", 8.0) or 8.0)
        delay = min(cap, base * (2 ** max(0, int(attempt))))
        return delay + random.uniform(0, delay * 0.4)

    # -- robots.txt -------------------------------------------------------

    def _robots_parser(self, url: str) -> RobotFileParser | None:
        """按 host 缓存 robots.txt 解析结果；抓不到时缓存 None（视为允许）。"""
        host = urlsplit(url).netloc.lower()
        if not host:
            return None
        with self._robots_lock:
            if host in self._robots_cache:
                return self._robots_cache[host]
        robots_url = site_root(url).rstrip("/") + "/robots.txt"
        parser: RobotFileParser | None = None
        try:
            self._throttle(url)
            response = self._session.get(
                robots_url,
                timeout=min(float(self.config.timeout_s or 15.0), 10.0),
                allow_redirects=True,
            )
            if response.status_code < 400 and response.content:
                encoding = detect_encoding(
                    response.content, response.headers.get("Content-Type", "")
                )
                text = decode_content(response.content, encoding)
                parser = RobotFileParser()
                parser.parse(text.splitlines())
        except requests.RequestException:
            parser = None
        except Exception:  # noqa: BLE001 - robots 解析异常同样视为允许
            parser = None
        with self._robots_lock:
            self._robots_cache[host] = parser
        return parser

    def _check_robots(self, url: str) -> None:
        if not self.config.respect_robots:
            return
        parser = self._robots_parser(url)
        if parser is None:
            return
        try:
            allowed = parser.can_fetch(self.config.user_agent or "*", url)
        except Exception:  # noqa: BLE001 - 解析异常时按允许处理
            return
        if not allowed:
            raise RobotsDenied(f"robots.txt 禁止抓取：{url}")

    # -- 静态抓取 ---------------------------------------------------------

    def _decode_response(self, response: Any, forced_encoding: str = "") -> tuple[str, str]:
        content = response.content or b""
        content_type = response.headers.get("Content-Type", "") or ""
        apparent: Callable[[], str] = lambda: getattr(response, "apparent_encoding", "") or ""
        encoding = detect_encoding(content, content_type, forced=forced_encoding, apparent=apparent)
        forced_name = ""
        if str(forced_encoding or "").strip().lower() not in ("", "auto"):
            forced_name = _normalize_encoding(forced_encoding)
        return decode_content(content, encoding), forced_name or encoding

    def _static_get(self, url: str, forced_encoding: str = "") -> FetchResult:
        """requests 静态抓取；网络/HTTP 失败返回 ok=False 的 FetchResult。"""
        started = time.monotonic()
        attempts = max(0, int(self.config.retries)) + 1
        last_error = ""
        for attempt in range(attempts):
            self._throttle(url)
            try:
                response = self._session.get(
                    url,
                    timeout=float(self.config.timeout_s or 15.0),
                    allow_redirects=True,
                )
            except requests.RequestException as error:
                last_error = f"{type(error).__name__}: {error}"
                if attempt + 1 < attempts:
                    delay = self._backoff_seconds(attempt)
                    self._emit(
                        f"请求失败，{delay:.1f}s 后重试（{attempt + 1}/{attempts - 1}）：{url}",
                        "fetching",
                    )
                    time.sleep(delay)
                    continue
                return FetchResult(
                    url=url,
                    final_url=url,
                    status=0,
                    method="http",
                    elapsed_ms=_elapsed_ms(started),
                    error=last_error[:500],
                )

            text, encoding = self._decode_response(response, forced_encoding)
            result = FetchResult(
                url=url,
                final_url=response.url or url,
                status=int(response.status_code),
                text=text,
                content=response.content or b"",
                headers={str(key).lower(): str(value) for key, value in response.headers.items()},
                encoding=encoding,
                method="http",
                elapsed_ms=_elapsed_ms(started),
            )
            if result.status in _RETRY_STATUS and attempt + 1 < attempts:
                last_error = f"HTTP {result.status}"
                delay = self._backoff_seconds(attempt)
                self._emit(
                    f"HTTP {result.status}，{delay:.1f}s 后重试（{attempt + 1}/{attempts - 1}）：{url}",
                    "fetching",
                )
                time.sleep(delay)
                continue
            if result.status >= 400:
                result.error = f"HTTP {result.status}"
            elif result.content_type.startswith(_BINARY_CONTENT_TYPES):
                result.error = f"非文本响应：{result.content_type}"
            elif not (text or "").strip():
                result.error = "响应内容为空"
            return result
        return FetchResult(
            url=url,
            final_url=url,
            status=0,
            method="http",
            elapsed_ms=_elapsed_ms(started),
            error=last_error[:500] or "抓取失败",
        )

    # -- 浏览器 -----------------------------------------------------------

    def _browser_proxy(self) -> dict[str, str] | None:
        """从环境变量读取 HTTP(S) 代理供 Chrome 使用。

        requests 默认 trust_env 会走系统代理，但 Playwright 启动的 Chrome 不读
        HTTP_PROXY 环境变量；不显式传入时在需要代理出网的机器上浏览器回退必然失败。
        """
        server = ""
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            value = str(os.environ.get(name) or "").strip()
            if value:
                server = value
                break
        configured = str(self.fetch_settings.get("proxy") or "").strip()
        if configured:
            server = configured
        if not server:
            return None
        proxy = {"server": server}
        bypass = (
            str(self.fetch_settings.get("proxy_bypass") or "").strip()
            or str(os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").strip()
        )
        if bypass:
            proxy["bypass"] = bypass
        return proxy

    def _ensure_browser(self) -> bool:
        """延迟启动 Playwright 浏览器（Chrome 优先，失败降级默认 chromium）。"""
        with self._browser_lock:
            if self._closed:
                return False
            if self._browser is not None:
                return True
            if self._browser_error:
                return False
            try:
                from playwright.sync_api import sync_playwright
            except Exception as error:  # noqa: BLE001 - 未安装时降级静态抓取
                self._browser_error = f"Playwright 不可用：{error}"
                return False
            last_error = ""
            try:
                self._playwright = sync_playwright().start()
                channel = str(self.fetch_settings.get("browser_channel") or "chrome").strip()
                attempts: list[dict[str, Any]] = [{"channel": channel}] if channel else []
                attempts.append({})
                for kwargs in attempts:
                    try:
                        self._browser = self._playwright.chromium.launch(
                            headless=bool(self.config.headless),
                            args=[
                                "--disable-blink-features=AutomationControlled",
                                "--disable-dev-shm-usage",
                                "--no-first-run",
                            ],
                            **kwargs,
                        )
                        break
                    except Exception as error:  # noqa: BLE001 - 换下一个启动参数
                        last_error = f"{type(error).__name__}: {error}"
                if self._browser is None:
                    raise RuntimeError(last_error or "浏览器启动失败")
                context_options: dict[str, Any] = {
                    "user_agent": self.config.user_agent or None,
                    "viewport": {"width": 1366, "height": 900},
                    "locale": "en-US",
                    "ignore_https_errors": bool(
                        self.fetch_settings.get("ignore_https_errors", True)
                    ),
                    "extra_http_headers": {"Accept-Language": BROWSER_ACCEPT_LANGUAGE},
                }
                proxy = self._browser_proxy()
                if proxy:
                    context_options["proxy"] = proxy
                self._context = self._browser.new_context(**context_options)
                self._context.set_default_timeout(int(float(self.config.timeout_s or 15.0) * 1000))
                return True
            except Exception as error:  # noqa: BLE001 - 浏览器整体不可用时降级
                self._browser_error = f"浏览器启动失败：{last_error or error}"
                self._close_browser()
                return False

    def _close_browser(self) -> None:
        context, browser, playwright = self._context, self._browser, self._playwright
        self._context = None
        self._browser = None
        self._playwright = None
        if context is not None:
            try:
                context.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:  # noqa: BLE001
                pass

    def _detect_challenge(self, page: Any) -> str:
        """检测验证码 / Cloudflare 人机验证特征；返回命中的描述，未命中返回空串。"""
        for selector in _CHALLENGE_SELECTORS:
            try:
                if page.query_selector(selector) is not None:
                    return f"页面元素 {selector}"
            except Exception:  # noqa: BLE001 - 元素查询失败忽略
                continue
        title = ""
        try:
            title = (page.title() or "").lower()
        except Exception:  # noqa: BLE001
            title = ""
        for marker in _CHALLENGE_MARKERS:
            if marker in title:
                return f"标题特征「{title[:60]}」"
        body = ""
        try:
            body = (page.inner_text("body") or "")[:3000].lower()
        except Exception:  # noqa: BLE001
            body = ""
        for marker in _CHALLENGE_MARKERS:
            if marker in body:
                return f"正文特征「{marker}」"
        return ""

    def _wait_intervention(self, challenge: str) -> None:
        """可见浏览器模式下等待用户处理验证码：on_event 通知 + 等待 intervention_event。"""
        timeout = float(self.fetch_settings.get("intervention_timeout_s", 180) or 180)
        self._emit(
            f"检测到疑似人机验证 / 拦截（{challenge}），"
            "请在浏览器窗口内处理，完成后点击“继续”……",
            "waiting_intervention",
        )
        event = self.intervention_event
        if event is None:
            return
        try:
            event.wait(timeout=max(5.0, timeout))
        except Exception:  # noqa: BLE001 - 事件对象异常不应中断抓取
            return

    def _browser_get(self, url: str, wait_selector: str = "", scroll: int = 0) -> FetchResult:
        """Playwright 浏览器抓取；任何异常都转成 ok=False 的 FetchResult。"""
        started = time.monotonic()
        if not self._ensure_browser():
            return FetchResult(
                url=url,
                final_url=url,
                status=0,
                method="browser",
                elapsed_ms=_elapsed_ms(started),
                error=f"浏览器不可用：{self._browser_error}"[:500],
            )
        self._throttle(url)
        self._emit(f"使用浏览器抓取：{url}", "fetching")
        context = self._context
        page = None
        try:
            page = context.new_page()
            response = None
            nav_error = ""
            try:
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=max(5000, int(float(self.config.timeout_s or 15.0) * 1000)),
                )
            except Exception as error:  # noqa: BLE001 - 超时后页面可能已有内容，继续读取
                nav_error = f"{type(error).__name__}: {error}"
                self._emit(f"页面加载未完成，尝试读取已有内容：{nav_error}"[:200], "fetching")
            status = int(getattr(response, "status", 200) or 200) if response is not None else 200
            current_url = ""
            try:
                current_url = page.url or ""
            except Exception:  # noqa: BLE001
                current_url = ""
            if current_url.startswith("chrome-error://") or (
                nav_error and not current_url.startswith(("http://", "https://"))
            ):
                return FetchResult(
                    url=url,
                    final_url=current_url or url,
                    status=0,
                    method="browser",
                    elapsed_ms=_elapsed_ms(started),
                    error=f"浏览器导航失败：{nav_error or current_url}"[:500],
                )
            if wait_selector:
                try:
                    page.wait_for_selector(
                        wait_selector,
                        timeout=min(15000, int(self.config.browser_wait_ms or 0) + 8000),
                    )
                except Exception:  # noqa: BLE001 - 等待选择器不是必需
                    pass
            wait_ms = max(0, int(self.config.browser_wait_ms or 0))
            if wait_ms:
                page.wait_for_timeout(wait_ms)
            for _ in range(max(0, int(scroll or 0))):
                try:
                    page.evaluate("window.scrollBy(0, window.innerHeight)")
                except Exception:  # noqa: BLE001
                    try:
                        page.mouse.wheel(0, 900)
                    except Exception:  # noqa: BLE001
                        pass
                page.wait_for_timeout(600)

            error_note = ""
            challenge = self._detect_challenge(page)
            if challenge:
                if self.config.headless:
                    error_note = f"疑似人机验证 / 拦截（headless 模式无法人工介入）：{challenge}"
                else:
                    self._wait_intervention(challenge)
                    page.wait_for_timeout(1200)

            content = ""
            try:
                content = page.content() or ""
            except Exception as error:  # noqa: BLE001
                return FetchResult(
                    url=url,
                    final_url=url,
                    status=status,
                    method="browser",
                    elapsed_ms=_elapsed_ms(started),
                    error=f"读取页面内容失败：{error}"[:500],
                )
            final_url = url
            try:
                final_url = page.url or url
            except Exception:  # noqa: BLE001
                final_url = url
            if final_url.startswith("chrome-error://"):
                error_note = _merge_errors(error_note, f"浏览器导航失败：{nav_error or final_url}")
            elif not content.strip():
                error_note = _merge_errors(error_note, "浏览器返回空页面")
            return FetchResult(
                url=url,
                final_url=final_url,
                status=status,
                text=content,
                content=content.encode("utf-8", "ignore"),
                headers={},
                encoding="utf-8",
                method="browser",
                elapsed_ms=_elapsed_ms(started),
                error=error_note,
            )
        except Exception as error:  # noqa: BLE001 - 浏览器异常统一转失败结果
            return FetchResult(
                url=url,
                final_url=url,
                status=0,
                method="browser",
                elapsed_ms=_elapsed_ms(started),
                error=f"浏览器抓取异常：{error}"[:500],
            )
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass

    # -- 静态结果是否像 JS 空壳 -------------------------------------------

    def _looks_js_shell(self, result: FetchResult) -> bool:
        """判断静态 HTML 是否只是 JS 应用外壳，需要浏览器重抓。"""
        html = result.text or ""
        if not html.strip():
            return True
        visible = _visible_text(html)
        visible_len = len(visible)
        html_len = len(html)
        if visible_len < 200 and html_len > 3000:
            return True
        if visible_len < 600 and html_len > 20000:
            return True
        lowered = html[:8000].lower()
        if any(marker in lowered for marker in _JS_SHELL_MARKERS):
            return True
        if re.search(
            r'<div[^>]+id=["\'](?:app|root|__next|__nuxt)["\'][^>]*>\s*</div>', html, re.I
        ):
            return True
        return False

    # -- 对外接口 ---------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        browser: bool | None = None,
        wait_selector: str = "",
        encoding: str = "",
        scroll: int = 0,
    ) -> FetchResult:
        """抓取一个 URL（静态优先、按需浏览器回退）。

        browser=None  按 config.browser_mode 与 JS 空壳判定；
        browser=True  强制浏览器；browser=False 只用静态请求；
        scroll>0      浏览器模式下滚动次数，用于触发懒加载列表。
        """
        prepared = self._prepare_url(url)
        if browser is True:
            mode = "browser"
        elif browser is False:
            mode = "static"
        else:
            mode = str(self.config.browser_mode or "auto").lower()
            if mode not in ("auto", "always", "never"):
                mode = "auto"
            mode = {"always": "browser", "never": "static"}.get(mode, "auto")

        cache_key: tuple[Any, ...] = (prepared, mode, wait_selector, int(scroll or 0), encoding)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # robots 检查对静态/浏览器都生效；被禁止时抛 RobotsDenied 由调用方记录警告。
        self._check_robots(prepared)
        started = time.monotonic()
        scroll_times = max(0, int(scroll or 0))

        if mode == "static":
            result = self._static_get(prepared, encoding)
        elif mode == "browser":
            result = self._browser_get(prepared, wait_selector, scroll_times)
            if not result.ok:
                static_result = self._static_get(prepared, encoding)
                if static_result.ok:
                    static_result.error = (
                        f"浏览器抓取失败，已降级为静态抓取：{result.error}"[:500]
                    )
                    result = static_result
                else:
                    result.error = _merge_errors(result.error, static_result.error)
        else:  # auto
            result = self._static_get(prepared, encoding)
            need_browser = False
            reason = ""
            if result.ok:
                if self._looks_js_shell(result):
                    need_browser = True
                    reason = "静态页面疑似 JS 空壳"
            elif int(result.status or 0) == 0:
                # 网络层失败（超时 / 连接重置 / TLS）值得换浏览器再试一次。
                need_browser = True
                reason = "静态抓取网络失败"
            elif int(result.status or 0) in _BROWSER_RETRY_STATUS:
                # 反爬/限流语义（如 Le Monde 对 requests 返回 406）：浏览器可能可抓。
                need_browser = True
                reason = f"静态抓取被拒绝（HTTP {result.status}），改用浏览器"
            if need_browser:
                self._emit(f"{reason}，尝试浏览器抓取：{prepared}", "fetching")
                browser_result = self._browser_get(prepared, wait_selector, scroll_times)
                if browser_result.ok:
                    result = browser_result
                else:
                    result.error = _merge_errors(result.error, browser_result.error)

        result.url = prepared
        result.elapsed_ms = _elapsed_ms(started)
        if result.ok:
            self._cache[cache_key] = result
        return result

    def render_svg_screenshot(self, svg: str, width: int, height: int) -> bytes | None:
        """用浏览器把 SVG 渲染成 PNG 字节（供 output.py 生成流程图 PNG）；失败返回 None。"""
        if not svg or not str(svg).strip():
            return None
        if not self._ensure_browser():
            return None
        page = None
        try:
            page = self._context.new_page()
            page.set_viewport_size(
                {"width": max(120, int(width or 120)), "height": max(120, int(height or 120))}
            )
            html = (
                "<!doctype html><html><head><meta charset=\"utf-8\">"
                "<style>html,body{margin:0;padding:0;background:#ffffff;}</style></head>"
                f"<body>{svg}</body></html>"
            )
            page.set_content(html, wait_until="load")
            page.wait_for_timeout(120)
            png = page.screenshot(full_page=True, type="png")
            return png or None
        except Exception:  # noqa: BLE001 - 渲染失败由调用方兜底
            return None
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass

    def close(self) -> None:
        """释放浏览器进程与 requests 会话；重复调用安全。"""
        self._closed = True
        with self._browser_lock:
            self._close_browser()
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass
        self._cache.clear()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
