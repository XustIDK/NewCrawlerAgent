# -*- coding: utf-8 -*-
"""真实新闻站点 HTML fixture 采集脚本（可选工具，需联网，不属于单元测试）。

用法（项目根目录）：

    .venv\\Scripts\\python.exe tests\\fixtures\\collect.py

设计：
- 保存**原始响应字节**（不解码、不改写），由 manifest.json 记录每份 fixture 的
  来源 URL / 解码编码 / 字节数 / sha256 / 采集时间 / 类型；
- 从各站点列表页用站点专属规则挑选文章链接，抓取前校验 200 且长度 > 15KB；
- 单站点/单文章失败自动重试并跳过，不中断其它站点；
- 末尾重写 tests/fixtures/manifest.json 并打印汇总（只输出 ASCII，避免终端乱码）。

产物供 tests/ 下离线单元测试使用；不参与 unittest 自动发现。
"""

from __future__ import annotations

import codecs
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

try:
    import requests
except ImportError:  # pragma: no cover - 采集工具依赖 requests
    sys.exit("缺少 requests：请使用 .venv 中的 python 运行本脚本")

FIXTURES_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = FIXTURES_DIR / "manifest.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7,ja;q=0.7,de;q=0.6,fr;q=0.5"
TIMEOUT_S = 30
ATTEMPTS = 4
MIN_ARTICLE_BYTES = 15 * 1024
MIN_FEED_BYTES = 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
POLITE_DELAY_S = 0.8

_ENCODING_ALIASES = {
    "utf8": "utf-8",
    "utf-8": "utf-8",
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "gb-2312": "gb18030",
    "x-gbk": "gb18030",
    "gb18030": "gb18030",
    "shift-jis": "shift_jis",
    "shift_jis": "shift_jis",
    "sjis": "shift_jis",
    "euc-kr": "euc-kr",
    "euckr": "euc-kr",
    "iso-8859-1": "windows-1252",
    "latin-1": "windows-1252",
}
_FALLBACK_ENCODINGS = ("utf-8", "gb18030", "shift_jis", "euc-kr", "windows-1252")

LIST_TARGETS = [
    {
        "site": "bbc",
        "name": "bbc_news_list",
        "url": "https://www.bbc.com/news",
        "kind": "list",
        "articles": 2,
        "notes": "BBC News 首页/列表页（英文，UTF-8）",
    },
    {
        "site": "dw",
        "name": "dw_top_list",
        "url": "https://www.dw.com/en/top-stories/s-9097",
        "kind": "list",
        "articles": 2,
        "notes": "DW 英文 Top Stories 列表页（德媒英文版）",
    },
    {
        "site": "nhk",
        "name": "nhk_news_list",
        "url": "https://www3.nhk.or.jp/news/",
        "kind": "list",
        "articles": 1,
        "notes": "NHK 新闻列表页（日文，302 到 news.web.nhk/newsweb）",
    },
    {
        "site": "lemonde",
        "name": "lemonde_en_list",
        "url": "https://www.lemonde.fr/en/",
        "kind": "list",
        "articles": 2,
        "notes": "Le Monde 英文版列表页（法媒英文版）",
    },
    {
        "site": "sina",
        "name": "sina_news_list",
        "url": "https://news.sina.com.cn/",
        "kind": "list",
        "articles": 2,
        "notes": "新浪新闻首页/列表页（中文，UTF-8；原始字节保存）",
    },
    {
        "site": "zaobao",
        "name": "zaobao_list",
        "url": "https://www.zaobao.com.sg/",
        "kind": "list",
        "articles": 1,
        "notes": "联合早报首页/列表页（简体中文，新加坡）",
    },
]

FEED_TARGETS = [
    {
        "site": "bbc",
        "name": "bbc_rss",
        "url": "https://feeds.bbci.co.uk/news/rss.xml",
        "kind": "feed",
        "notes": "BBC News RSS 2.0 feed（feed 解析测试）",
    },
]

# 站点专属文章链接规则：基于 path/host 判定，避免把导航/栏目页当文章。
_ARTICLE_RULES = {
    "bbc": lambda path, host: bool(re.fullmatch(r"/news/articles/[a-z0-9]+", path))
    and host.endswith("bbc.com"),
    "dw": lambda path, host: bool(re.search(r"/a-\d+$", path))
    and host.endswith("dw.com"),
    "nhk": lambda path, host: host == "news.web.nhk"
    and bool(re.fullmatch(r"/newsweb/na/[a-z0-9-]+", path)),
    "lemonde": lambda path, host: bool(re.fullmatch(r"/en/[a-z-]+/article/\d{4}/\d{2}/\d{2}/[^/]+\.html", path)),
    "sina": lambda path, host: host == "news.sina.com.cn"
    and bool(re.fullmatch(r"/(?:[a-z]+/)*\d{4}-\d{2}-\d{2}/doc-[a-z0-9]+\.shtml", path)),
    "zaobao": lambda path, host: bool(re.fullmatch(r"/[a-z-]+/[a-z-]+/story\d{8}-\d+", path))
    and host.endswith("zaobao.com.sg"),
}

_RECORDS: list[dict] = []


def _log(message: str) -> None:
    ascii_message = message.encode("ascii", "replace").decode("ascii")
    print(f"[fixtures] {ascii_message}", flush=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_encoding(name: str) -> str:
    value = str(name or "").strip().lower().replace("_", "-")
    value = _ENCODING_ALIASES.get(value, value)
    try:
        codecs.lookup(value)
    except LookupError:
        return ""
    return value


def _declared_encodings(content: bytes, headers: dict) -> list[str]:
    names: list[str] = []
    head = content[:16384]
    for match in re.finditer(rb"<meta[^>]+charset\s*=\s*[\"']?([\w.:-]+)", head, re.I):
        names.append(match.group(1).decode("ascii", "ignore"))
    content_type = str((headers or {}).get("content-type", ""))
    match = re.search(r"charset\s*=\s*[\"']?([\w.:-]+)", content_type, re.I)
    if match:
        names.append(match.group(1))
    return names


def detect_encoding(content: bytes, headers: dict) -> str:
    """识别字节流编码：BOM -> meta/header 声明 -> UTF-8 -> 常见多字节 -> windows-1252。"""
    for bom, encoding in (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if content.startswith(bom):
            try:
                content.decode(encoding)
                return encoding
            except UnicodeDecodeError:
                break
    candidates = [name for name in _declared_encodings(content, headers) if _normalize_encoding(name)]
    for name in candidates:
        encoding = _normalize_encoding(name)
        try:
            content.decode(encoding)
            return encoding
        except (LookupError, UnicodeDecodeError):
            continue
    for encoding in _FALLBACK_ENCODINGS:
        try:
            content.decode(encoding)
            return encoding
        except (LookupError, UnicodeDecodeError):
            continue
    return "latin-1"


def _request(session: requests.Session, url: str, attempts: int = ATTEMPTS) -> requests.Response | None:
    last_error: Exception | None = None
    for index in range(attempts):
        try:
            response = session.get(url, timeout=TIMEOUT_S, allow_redirects=True)
            if response.status_code >= 500 and index + 1 < attempts:
                last_error = RuntimeError(f"HTTP {response.status_code}")
                time.sleep(1.0 + index)
                continue
            return response
        except Exception as error:  # noqa: BLE001 - 采集工具容错
            last_error = error
            time.sleep(1.0 + index)
    _log(f"ERROR {url} -> {type(last_error).__name__}: {str(last_error)[:120]}")
    return None


def _save_fixture(
    site: str,
    name: str,
    content: bytes,
    source_url: str,
    kind: str,
    notes: str,
    *,
    encoding: str = "",
) -> dict:
    site_dir = FIXTURES_DIR / site
    site_dir.mkdir(parents=True, exist_ok=True)
    relative_path = f"{site}/{name}.html"
    path = FIXTURES_DIR / relative_path
    path.write_bytes(content)
    used_encoding = encoding or detect_encoding(content, {})
    digest = hashlib.sha256(content).hexdigest()
    record = {
        "name": name,
        "path": relative_path,
        "source_url": source_url,
        "encoding_used": used_encoding,
        "bytes": len(content),
        "sha256": digest,
        "fetched_at": _utc_now(),
        "kind": kind,
        "notes": notes,
    }
    _RECORDS.append(record)
    _log(f"saved {relative_path} {len(content)}B encoding={used_encoding}")
    return record


def _absolute_links(html: str, base_url: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"']", html, re.I):
        href = match.group(1).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        urls.append(urljoin(base_url, href))
    return urls


def _article_candidates(site: str, html: str, base_url: str) -> list[str]:
    urls = _absolute_links(html, base_url)
    if site == "nhk":
        # NHK 新版列表页由 Nuxt 渲染，文章链接同时出现在内联 JSON 中。
        urls.extend(match.group(0) for match in re.finditer(
            r"https://news\.web\.nhk/newsweb/na/[a-z0-9-]+", html
        ))
    rule = _ARTICLE_RULES[site]
    seen: set[str] = set()
    candidates: list[str] = []
    for url in urls:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        path = parts.path or ""
        if not host or not path or url in seen:
            continue
        if not rule(path, host):
            continue
        cleaned = f"{parts.scheme}://{parts.netloc}{path}"
        if cleaned in seen:
            continue
        seen.add(url)
        seen.add(cleaned)
        candidates.append(cleaned)
    return candidates


def _collect_article(
    session: requests.Session,
    site: str,
    name: str,
    url: str,
    notes: str,
) -> dict | None:
    response = _request(session, url)
    if response is None:
        return None
    if response.status_code != 200:
        _log(f"skip {url} HTTP {response.status_code}")
        return None
    if len(response.content) < MIN_ARTICLE_BYTES:
        _log(f"skip {url} too small {len(response.content)}B")
        return None
    content_type = str(response.headers.get("content-type", "")).lower()
    if "html" not in content_type and "xml" not in content_type:
        _log(f"skip {url} content-type {content_type[:40]}")
        return None
    return _save_fixture(
        site=site,
        name=name,
        content=response.content,
        source_url=response.url or url,
        kind="article",
        notes=notes,
    )


def _collect_list_target(session: requests.Session, target: dict) -> None:
    site = target["site"]
    _log(f"fetching list {target['url']}")
    response = _request(session, target["url"])
    if response is None:
        _log(f"SKIP list {target['url']} (network failed)")
        return
    if response.status_code != 200:
        _log(f"SKIP list {target['url']} HTTP {response.status_code}")
        return
    list_record = _save_fixture(
        site=site,
        name=target["name"],
        content=response.content,
        source_url=response.url or target["url"],
        kind="list",
        notes=target["notes"],
    )
    encoding = list_record["encoding_used"]
    try:
        html = response.content.decode(encoding, "replace")
    except LookupError:
        html = response.content.decode("utf-8", "replace")
    candidates = _article_candidates(site, html, response.url or target["url"])
    _log(f"discovered {len(candidates)} article candidates for {site}")
    wanted = int(target.get("articles", 1))
    max_attempts = int(target.get("max_attempts", 8))
    collected = 0
    attempts = 0
    for url in candidates:
        if collected >= wanted or attempts >= max_attempts:
            break
        attempts += 1
        time.sleep(POLITE_DELAY_S)
        index = collected + 1
        article_notes = f"{target['site']} 文章页 {index}（来源：{target['name']} 列表）"
        record = _collect_article(session, site, f"{site}_article_{index}", url, article_notes)
        if record is not None:
            collected += 1
    if collected < wanted:
        _log(f"WARN {site}: collected {collected}/{wanted} article fixtures")
    return


def _collect_feed_target(session: requests.Session, target: dict) -> None:
    response = _request(session, target["url"])
    if response is None or response.status_code != 200:
        _log(f"SKIP feed {target['url']}")
        return
    if len(response.content) < MIN_FEED_BYTES:
        _log(f"SKIP feed {target['url']} too small")
        return
    _save_fixture(
        site=target["site"],
        name=target["name"],
        content=response.content,
        source_url=response.url or target["url"],
        kind=target["kind"],
        notes=target["notes"],
    )


def _make_synthetic_gbk_fixture() -> None:
    """把一份真实新浪文章按 GB18030 转码，供编码识别回归测试（线上新浪现为 UTF-8）。"""
    source = next(
        (record for record in _RECORDS if record["kind"] == "article" and record["path"].startswith("sina/")),
        None,
    )
    if source is None:
        _log("skip synthetic GBK fixture (no sina article)")
        return
    raw = (FIXTURES_DIR / source["path"]).read_bytes()
    try:
        text = raw.decode(source["encoding_used"])
    except (LookupError, UnicodeDecodeError):
        text = raw.decode("utf-8", "replace")
    text = re.sub(r"charset\s*=\s*[\"']?utf-?8", 'charset="gb18030"', text, count=5, flags=re.I)
    try:
        gbk_bytes = text.encode("gb18030", "xmlcharrefreplace")
    except UnicodeError:
        _log("skip synthetic GBK fixture (encode failed)")
        return
    _save_fixture(
        site="sina",
        name="sina_article_gbk",
        content=gbk_bytes,
        source_url=source["source_url"],
        kind="article",
        notes=(
            "合成 fixture：将上面的真实新浪文章按 GB18030 重新编码（并改写 meta charset），"
            "用于编码识别/解码回归测试；线上页面本身是 UTF-8。"
        ),
        encoding="gb18030",
    )


def main() -> int:
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": ACCEPT_LANGUAGE})
    if FIXTURES_DIR.exists():
        for old in FIXTURES_DIR.rglob("*.html"):
            old.unlink()
    started = time.time()
    _log(f"start collecting fixtures into {FIXTURES_DIR}")
    for target in LIST_TARGETS:
        _collect_list_target(session, target)
        time.sleep(POLITE_DELAY_S)
    for target in FEED_TARGETS:
        _collect_feed_target(session, target)
        time.sleep(POLITE_DELAY_S)
    _make_synthetic_gbk_fixture()

    total_bytes = sum(record["bytes"] for record in _RECORDS)
    manifest = {
        "version": 1,
        "generated_at": _utc_now(),
        "total_bytes": total_bytes,
        "fixtures": _RECORDS,
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _log(f"wrote {MANIFEST_PATH.name}: {len(_RECORDS)} fixtures, {total_bytes / 1024 / 1024:.2f} MB, "
         f"{int(time.time() - started)}s")
    if total_bytes > MAX_TOTAL_BYTES:
        _log(f"WARN total fixtures {total_bytes}B exceed budget {MAX_TOTAL_BYTES}B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
