"""URL 工具：入口校验、域名归一、同站判断、追踪参数清理。

这里只做与网络无关的纯字符串/URL 处理，便于单元测试。
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

# 常见追踪参数：归一化 canonical URL、去重时剔除，避免同一文章出现多个 URL。
TRACKING_PARAM_PREFIXES = (
    "utm_",
    "spm",
    "scm",
    "from",
    "share_",
    "share-",
    "fbclid",
    "gclid",
    "yclid",
    "msclkid",
    "igshid",
    "ref",
    "referrer",
    "cmpid",
    "ocid",
    "xtor",
    "_ga",
    "mc_cid",
    "mc_eid",
)
TRACKING_PARAMS = {
    "spm",
    "from",
    "ref",
    "referrer",
    "cmpid",
    "ocid",
    "xtor",
    "fbclid",
    "gclid",
    "yclid",
    "msclkid",
    "igshid",
    "_ga",
    "mc_cid",
    "mc_eid",
}

_NON_HTTP_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "about:", "ftp:", "#")


def is_http_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    parts = urlsplit(url.strip())
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def normalize_entry_url(raw: str) -> str:
    """把用户输入整理成可抓取的 http(s) URL。

    - 允许省略协议（example.com/news -> https://example.com/news）
    - 去掉首尾空白与片段（#anchor）
    - 拒绝非 http(s) 地址与空 host
    """
    if not isinstance(raw, str):
        raise ValueError("网址必须是字符串")
    value = raw.strip()
    if not value:
        raise ValueError("网址不能为空")
    if value.lower().startswith(
        ("javascript:", "mailto:", "tel:", "data:", "ftp:", "file:", "about:", "chrome:", "ws:", "wss:")
    ):
        raise ValueError(f"只支持 http/https 网址：{raw!r}")
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"只支持 http/https 网址：{raw!r}")
    if not parts.netloc:
        raise ValueError(f"网址缺少域名：{raw!r}")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def domain_from_url(url: str) -> str:
    """返回归一化域名：小写、IDNA、去端口、去前导 www。

    bbc.com / www.bbc.com / m.bbc.com 的父域都是 bbc.com，
    站点配置按“等于或后缀匹配”查找，见 profiles.load_profile()。
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        return ""
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    if host.startswith("www."):
        host = host[4:]
    return host


def site_root(url: str) -> str:
    """返回 scheme://netloc，用于拼相对链接/生成站点级 URL。"""
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def absolute_url(base_url: str, href: str) -> str:
    """相对链接转绝对链接；过滤 javascript/mailto/纯锚点等无效链接。"""
    if not href:
        return ""
    value = href.strip()
    low = value.lower()
    if low.startswith(_NON_HTTP_SCHEMES):
        return ""
    if value.startswith("//"):
        scheme = urlsplit(base_url).scheme or "https"
        value = f"{scheme}:{value}"
    elif "://" not in value:
        value = urljoin(base_url, value)
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def strip_tracking(url: str) -> str:
    """删除常见追踪参数并排序剩余参数，得到更稳定的 URL。"""
    parts = urlsplit(url.strip())
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
        and not key.lower().startswith(TRACKING_PARAM_PREFIXES)
    ]
    query = urlencode(sorted(kept))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def is_same_site(url: str, domain: str) -> bool:
    """url 是否属于 domain（含子域）。domain 需为 domain_from_url 归一后的形式。"""
    if not domain:
        return False
    host = domain_from_url(url)
    return bool(host) and (host == domain or host.endswith("." + domain))


def url_matches_any(url: str, patterns: list[str]) -> bool:
    """正则匹配 URL（path+query），用于 include / exclude 规则。"""
    if not patterns:
        return False
    value = urlsplit(url.strip())
    target = value.path + ("?" + value.query if value.query else "")
    for pattern in patterns:
        try:
            if re.search(pattern, target, re.IGNORECASE):
                return True
        except re.error:
            # 配置写错时退化为子串匹配，避免整个任务失败。
            if pattern.lower() in target.lower():
                return True
    return False
