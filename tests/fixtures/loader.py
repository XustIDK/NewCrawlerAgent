# -*- coding: utf-8 -*-
"""tests/fixtures 加载器：把离线 HTML/XML 快照按 manifest 记录的编码解码。

fixtures 由 tests/fixtures/collect.py 从真实新闻站点采集，保存的是
**原始响应字节**；manifest.json 记录每份快照的 name / path / source_url /
encoding_used / bytes / sha256 / fetched_at / kind / notes。

测试代码不联网，统一通过本模块读取 fixtures：

    from tests.fixtures.loader import load_fixture, fixture_names, list_fixtures

    fixture = load_fixture("bbc_news_list")
    assert fixture.kind == "list" and "news" in fixture.text

解码策略：先按 manifest 记录的 encoding_used 严格解码，失败再依次尝试
UTF-8 / GB18030 / Shift_JIS / EUC-KR / windows-1252 / latin-1，尽量不让
fixture 噪声影响被测代码。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = FIXTURES_DIR / "manifest.json"

# 兜底解码顺序：manifest 记录缺失/写错时仍能读出文本。
_FALLBACK_ENCODINGS = ("utf-8", "gb18030", "shift_jis", "euc-kr", "windows-1252", "latin-1")


class FixtureError(RuntimeError):
    """fixture 缺失、manifest 损坏或字节无法解码。"""


@dataclass(frozen=True)
class Fixture:
    """一份离线网页快照（HTML / XML / JSON 文本）。

    字段：
    - text       按记录编码解码后的文本，交给 dom.parse_html 等使用；
    - data / bytes  原始字节（测试编码识别、字节级校验时使用）；
    - url        采集时的最终来源 URL；
    - encoding   实际成功解码所用的编码名；
    - path       快照文件路径；
    - kind       list / article / feed；
    - notes      manifest 中的说明。
    """

    name: str
    path: Path
    url: str
    encoding: str
    text: str
    data: bytes
    kind: str
    notes: str = ""

    @property
    def bytes(self) -> bytes:
        """原始字节（保留站点编码，如 GB18030）。"""
        return self.data

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def is_html(self) -> bool:
        return self.kind in ("list", "article")


def _manifest_path(manifest_path: Path | str | None = None) -> Path:
    return Path(manifest_path) if manifest_path else MANIFEST_PATH


def load_manifest(manifest_path: Path | str | None = None) -> dict[str, Any]:
    """读取并校验 manifest.json，返回完整 dict（含 version / total_bytes / fixtures）。"""
    path = _manifest_path(manifest_path)
    if not path.is_file():
        raise FixtureError(f"fixture manifest 不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FixtureError(f"fixture manifest 无法解析：{path}（{error}）") from error
    if not isinstance(data, dict) or not isinstance(data.get("fixtures"), list):
        raise FixtureError(f"fixture manifest 结构非法：{path}")
    return data


def _entries(manifest_path: Path | str | None = None) -> list[dict[str, Any]]:
    entries = load_manifest(manifest_path).get("fixtures") or []
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("name")]


def _find_entry(name: str, manifest_path: Path | str | None = None) -> dict[str, Any]:
    for entry in _entries(manifest_path):
        if entry.get("name") == name:
            return entry
    available = ", ".join(entry["name"] for entry in _entries(manifest_path))
    raise FixtureError(f"未知 fixture：{name!r}；可用 fixtures：{available}")


def fixture_names(kind: str | None = None, manifest_path: Path | str | None = None) -> list[str]:
    """返回全部 fixture 名称；可按 kind（list/article/feed）过滤。"""
    return [
        entry["name"]
        for entry in _entries(manifest_path)
        if kind is None or entry.get("kind") == kind
    ]


def list_fixtures(manifest_path: Path | str | None = None) -> list[dict[str, Any]]:
    """返回 manifest 条目列表（浅拷贝，不含文件内容），用于遍历/断言。"""
    return [dict(entry) for entry in _entries(manifest_path)]


def fixture_path(name: str, manifest_path: Path | str | None = None) -> Path:
    """返回 fixture 的绝对路径；文件缺失时抛 FixtureError。"""
    entry = _find_entry(name, manifest_path)
    path = FIXTURES_DIR / str(entry.get("path") or "")
    if not path.is_file():
        raise FixtureError(f"fixture 文件缺失：{name} -> {path}")
    return path


def decode_bytes(content: bytes, encoding: str = "") -> tuple[str, str]:
    """按首选编码解码字节，失败后按兜底顺序重试；返回 (文本, 实际编码)。"""
    candidates: list[str] = []
    if encoding:
        candidates.append(str(encoding))
    candidates.extend(item for item in _FALLBACK_ENCODINGS if item not in candidates)
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            return content.decode(candidate), candidate
        except (LookupError, UnicodeDecodeError) as error:
            last_error = error
            continue
    raise FixtureError(f"fixture 字节无法解码（首选编码 {encoding!r}）：{last_error}")


def load_fixture(name: str, manifest_path: Path | str | None = None) -> Fixture:
    """读取一份 fixture：原始字节 + 解码文本 + manifest 元数据。"""
    entry = _find_entry(name, manifest_path)
    path = fixture_path(name, manifest_path)
    try:
        content = path.read_bytes()
    except OSError as error:
        raise FixtureError(f"fixture 读取失败：{name}（{error}）") from error
    text, encoding = decode_bytes(content, str(entry.get("encoding_used") or ""))
    return Fixture(
        name=str(entry["name"]),
        path=path,
        url=str(entry.get("source_url") or ""),
        encoding=encoding,
        text=text,
        data=content,
        kind=str(entry.get("kind") or ""),
        notes=str(entry.get("notes") or ""),
    )


def fixture_text(name: str, manifest_path: Path | str | None = None) -> str:
    """便捷函数：只取解码后的文本。"""
    return load_fixture(name, manifest_path).text


def fixtures_by_kind(kind: str, manifest_path: Path | str | None = None) -> list[Fixture]:
    """按 kind 批量加载（例如一次取出全部 article fixtures）。"""
    return [load_fixture(name, manifest_path) for name in fixture_names(kind, manifest_path)]
