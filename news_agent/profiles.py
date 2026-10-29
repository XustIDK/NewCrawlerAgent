"""站点画像（sites/<domain>.json）的读写、校验与域名匹配。

站点配置是可选的：没有配置时抽取层走通用策略；有了配置优先用配置里的
CSS/XPath 选择器（语法见 news_agent/dom.py）。本模块只负责文件与结构，
不负责网络请求。

域名匹配规则：host 等于配置 domain 或以 ".domain" 结尾（含子域），
多个匹配时取最长 domain（例如同时存在 bbc.com 与 news.bbc.com 时优先后者）。
"""

from __future__ import annotations

import json
import warnings
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import SITES_DIR
from .errors import ProfileError
from .models import (
    DEFAULT_ARTICLE,
    DEFAULT_DISCOVERY,
    DEFAULT_FETCH,
    DEFAULT_LIST,
    SiteProfile,
)
from .urls import domain_from_url

PROFILE_SUFFIX = ".json"
TEMPLATE_FILENAME = "_template.json"

_ARTICLE_SELECTOR_KEYS = ("title", "body", "date", "author", "source", "summary", "section")
_LIST_SELECTOR_KEYS = ("url", "item", "item_title", "item_date", "item_section", "item_image")
_STRING_KEYS = ("name", "language", "note", "learned_by", "updated_at")


def normalize_domain(value: str) -> str:
    """把 URL 或纯域名归一化成站点域（小写 IDNA、去端口、去前导 www）。"""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    try:
        return domain_from_url(text)
    except ValueError:
        return ""


def _sites_path(sites_dir: Path | str | None = None) -> Path:
    return Path(sites_dir) if sites_dir else Path(SITES_DIR)


def profile_path(domain: str, sites_dir: Path | str | None = None) -> Path:
    """返回站点配置路径：sites/<归一化域名>.json。"""
    host = normalize_domain(domain)
    if not host:
        raise ProfileError(f"站点域名无效：{domain!r}")
    return _sites_path(sites_dir) / f"{host}{PROFILE_SUFFIX}"


def _validate_selector_group(data: dict[str, Any], group: str, keys: tuple[str, ...]) -> None:
    value = data.get(group)
    if value is None:
        return
    if not isinstance(value, dict):
        raise ProfileError(f"站点配置 {group} 必须是 JSON 对象")
    for key in keys:
        item = value.get(key)
        if item is not None and not isinstance(item, str):
            raise ProfileError(f"站点配置 {group}.{key} 必须是选择器字符串")
    if group == "article":
        exclude = value.get("exclude")
        if exclude is not None and (
            not isinstance(exclude, list) or any(not isinstance(entry, str) for entry in exclude)
        ):
            raise ProfileError("站点配置 article.exclude 必须是选择器字符串数组")
    if group == "list":
        pagination = value.get("pagination")
        if pagination is not None and not isinstance(pagination, dict):
            raise ProfileError("站点配置 list.pagination 必须是 JSON 对象")
        if isinstance(pagination, dict):
            for key in ("next", "url_template"):
                item = pagination.get(key)
                if item is not None and not isinstance(item, str):
                    raise ProfileError(f"站点配置 list.pagination.{key} 必须是字符串")
            max_pages = pagination.get("max_pages")
            if max_pages is not None and not isinstance(max_pages, int):
                raise ProfileError("站点配置 list.pagination.max_pages 必须是整数")


def validate_profile(data: dict) -> SiteProfile:
    """校验站点配置结构并构造 SiteProfile；非法时抛 ProfileError（消息具体）。"""
    if not isinstance(data, dict):
        raise ProfileError("站点配置必须是 JSON 对象")
    raw_domain = data.get("domain")
    if not isinstance(raw_domain, str) or not raw_domain.strip():
        raise ProfileError("站点配置缺少有效 domain（例如 bbc.com）")
    domain = normalize_domain(raw_domain)
    if not domain or "." not in domain:
        raise ProfileError(f"站点配置 domain 非法：{raw_domain!r}")
    payload = dict(data)
    payload["domain"] = domain
    for key in _STRING_KEYS:
        value = payload.get(key)
        if value is not None and not isinstance(value, str):
            raise ProfileError(f"站点配置 {key} 必须是字符串")
    for key in ("include", "exclude"):
        value = payload.get(key)
        if value is not None and (
            not isinstance(value, list) or any(not isinstance(entry, str) for entry in value)
        ):
            raise ProfileError(f"站点配置 {key} 必须是字符串数组")
    _validate_selector_group(payload, "fetch", ())
    _validate_selector_group(payload, "discovery", ())
    _validate_selector_group(payload, "list", _LIST_SELECTOR_KEYS)
    _validate_selector_group(payload, "article", _ARTICLE_SELECTOR_KEYS)
    try:
        profile = SiteProfile.from_dict(payload)
    except (TypeError, ValueError) as error:
        raise ProfileError(f"站点配置非法：{error}") from error
    return profile


def _load_file(path: Path) -> SiteProfile | None:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        warnings.warn(
            f"站点配置读取失败，已忽略 {path.name}：{error}", RuntimeWarning, stacklevel=3
        )
        return None
    try:
        return validate_profile(data)
    except ProfileError as error:
        warnings.warn(
            f"站点配置非法，已忽略 {path.name}：{error}", RuntimeWarning, stacklevel=3
        )
        return None


def _iter_profile_files(sites_dir: Path | str | None = None) -> list[Path]:
    directory = _sites_path(sites_dir)
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.glob(f"*{PROFILE_SUFFIX}")
        if path.name != TEMPLATE_FILENAME and path.is_file()
    )


def _matches(host: str, profile_domain: str) -> bool:
    return bool(host) and (
        host == profile_domain or host.endswith("." + profile_domain)
    )


def load_profile(domain: str, sites_dir: Path | str | None = None) -> SiteProfile | None:
    """按域名加载站点配置；没有匹配时返回 None，坏配置只告警不崩溃。"""
    host = normalize_domain(domain)
    if not host:
        return None
    best: tuple[int, SiteProfile] | None = None
    for path in _iter_profile_files(sites_dir):
        profile = _load_file(path)
        if profile is None:
            continue
        if _matches(host, profile.domain):
            if best is None or len(profile.domain) > best[0]:
                best = (len(profile.domain), profile)
    return best[1] if best else None


def load_profile_for_url(url: str, sites_dir: Path | str | None = None) -> SiteProfile | None:
    """按页面 URL 加载站点配置（入口层便捷函数）。"""
    return load_profile(domain_from_url(url), sites_dir)


def save_profile(profile: SiteProfile | dict, sites_dir: Path | str | None = None) -> Path:
    """校验并保存站点配置（UTF-8、indent=2、ensure_ascii=False），返回文件路径。"""
    if isinstance(profile, SiteProfile):
        validated = profile
    elif isinstance(profile, dict):
        validated = validate_profile(profile)
    else:
        raise ProfileError("save_profile 需要 SiteProfile 或 dict")
    path = profile_path(validated.domain, sites_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    validated.updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    text = json.dumps(validated.to_dict(), ensure_ascii=False, indent=2)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def list_profiles(sites_dir: Path | str | None = None) -> list[dict]:
    """列出全部可用站点配置，供网页/CLI/Agent 展示。"""
    result: list[dict] = []
    for path in _iter_profile_files(sites_dir):
        profile = _load_file(path)
        if profile is None:
            continue
        result.append({
            "domain": profile.domain,
            "name": profile.name,
            "language": profile.language,
            "path": str(path),
            "learned_by": profile.learned_by,
            "updated_at": profile.updated_at,
        })
    return result


def profile_template() -> dict:
    """返回站点配置模板：优先 sites/_template.json，缺失时用内置默认值。"""
    path = Path(SITES_DIR) / TEMPLATE_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data:
            return data
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    article = deepcopy(DEFAULT_ARTICLE)
    article.update({
        "title": "h1",
        "body": "article p",
        "date": "time::attr(datetime)",
        "author": "[rel=author]",
        "source": "meta[property=og:site_name]::attr(content)",
        "summary": "meta[name=description]::attr(content)",
        "exclude": [".ad", ".related", ".newsletter", ".share"],
    })
    listing = deepcopy(DEFAULT_LIST)
    listing.update({
        "item": "article a[href]::attr(href)",
        "item_title": "article h2, article h3",
        "item_date": "article time::attr(datetime)",
        "item_image": "article img::attr(src)",
    })
    listing["pagination"] = {"next": "a[rel=next]::attr(href)", "max_pages": 3}
    return {
        "domain": "example.com",
        "name": "Example News",
        "language": "en",
        "fetch": deepcopy(DEFAULT_FETCH),
        "discovery": deepcopy(DEFAULT_DISCOVERY),
        "list": listing,
        "article": article,
        "include": [],
        "exclude": ["/video/", "/live/"],
        "note": "站点配置模板：复制为 sites/<domain>.json 后按需填写选择器。",
        "learned_by": "human",
    }
