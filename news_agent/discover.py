"""新闻列表发现：站点画像 / RSS-Atom / JSON-LD / 链接聚类 / 翻页。

对应 DESIGN.md §4.3 / §9.2 的契约，五层策略按优先级依次尝试，
命中足够条目即止，结果统一为 NewsItem 列表（url/title/published_raw/section/
image/list_url/method），按归一化 URL 去重后截断到 max_candidates。

- profile  ：sites/<domain>.json 的 list.item 选择器 + 翻页规则；
- feed     ：画像 feeds + <link rel=alternate> + 常见后缀探测，标准库 xml.etree 解析；
- jsonld   ：ItemList / CollectionPage / WebPage.mainEntity / @graph 文章对象；
- heuristic：收集同站链接，按“最近祖先的重复子结构签名 + 标题/日期/标题标签上下文”打分，
             选出最像新闻列表的容器；
- 翻页     ：rel=next / 多语言 next 文本 / URL 页码模式递增，最多 max_list_pages 页。

probe_structure() 面向 Agent 的 inspect_site 工具，返回 JSON 可序列化、总长 < 8KB 的
站点结构报告（feeds/JSON-LD 类型/候选选择器/文章链接样例/翻页提示/文章页字段探测）。
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from lxml import etree

from . import dom
from .fetch import Fetcher
from .models import CrawlConfig, NewsItem, SiteProfile
from .urls import (
    absolute_url,
    domain_from_url,
    is_same_site,
    site_root,
    strip_tracking,
    url_matches_any,
)

__all__ = [
    "DiscoveryResult",
    "discover_news",
    "probe_structure",
    "candidate_link_selectors",
    "looks_like_article_url",
    "extract_feed_items",
    "discover_feeds",
]

# 常见 feed 后缀：未显式配置、页面也没声明时按顺序探测（命中即止）。
DEFAULT_FEED_SUFFIXES = (
    "/feed",
    "/rss",
    "/rss.xml",
    "/feed.xml",
    "/atom.xml",
    "/index.xml",
    "/news/feed",
    "/news/rss",
)

# 明显不是文章路径的段（精确匹配，避免误伤 /articles/ 这类正常前缀）。
_BAD_SEGMENTS = frozenset(
    {
        "login", "signin", "sign-in", "logout", "signup", "sign-up", "register",
        "account", "accounts", "profile", "settings", "subscribe", "subscription",
        "subscriptions", "newsletter", "search", "tag", "tags", "topic", "topics",
        "author", "authors", "byline", "person", "people", "about", "about-us",
        "privacy", "privacy-policy", "terms", "terms-of-service", "terms-of-use",
        "contact", "contact-us", "cookies", "cookie-policy", "faq", "help",
        "careers", "jobs", "advertise", "advertising", "sitemap", "rss", "feed",
        "feeds", "feed.xml", "rss.xml", "atom.xml", "index.xml", "video", "videos",
        "live", "livestream", "live-stream", "gallery", "galleries", "photos",
        "photo", "podcast", "podcasts", "weather", "shop", "store", "my-account",
        "user", "users", "comment", "comments", "404", "error", "not-found",
        "index", "index.html", "index.htm", "home", "default.aspx", "amp",
    }
)

_BAD_EXTENSIONS = frozenset(
    {
        "jpg", "jpeg", "png", "gif", "webp", "svg", "ico", "bmp", "tiff",
        "mp4", "m4v", "mov", "avi", "wmv", "mp3", "m4a", "wav", "ogg", "flac",
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "zip", "rar", "7z",
        "gz", "tar", "css", "js", "json", "xml", "rss", "atom", "ics", "apk",
        "exe", "dmg", "iso",
    }
)

_ARTICLE_EXTENSIONS = ("html", "htm", "shtml", "xhtml", "php", "aspx", "jsp")

# 导航/页脚等容器标记：启发式聚类时降低权重或直接跳过其链接。
_NOISE_MARKERS = (
    "menu", "footer", "sidebar", "breadcrumb", "social", "share", "related",
    "recommend", "comment", "cookie", "subscribe", "login", "masthead",
    "topbar", "pagination", "pager", "advert", "promo", "banner", "toolbar",
)
_NAV_MARKERS = _NOISE_MARKERS + ("nav", "header")

# 多语言“下一页”关键词（文本 / aria-label 命中）。
_NEXT_KEYWORDS = (
    "next", "next page", "older", "older posts", "下一页", "下页", "下一頁",
    "后一页", "次へ", "次ページ", "次のページ", "suivant", "suivante",
    "page suivante", "weiter", "nächste", "nächste seite", "siguiente",
    "página siguiente", "»", "›", ">",
)

# 频道/栏目首页路径段：即使出现在候选里也要过滤掉（它们是导航页不是文章）。
_CHANNEL_SEGMENTS = frozenset(
    {
        "news", "world", "business", "sport", "sports", "politics", "tech",
        "technology", "science", "health", "entertainment", "culture",
        "lifestyle", "opinion", "editorial", "china", "international",
        "finance", "economy", "education", "travel", "auto", "autos",
        "military", "society", "local", "national", "global", "video",
        "videos", "photos", "podcast", "podcasts", "weather", "homepage",
        "home", "main", "index", "top-stories", "latest", "realtime",
    }
)

# feed 候选地址里的噪声特征：反馈/评论/登录/订阅页常被误标为 RSS。
_FEED_NOISE_MARKERS = (
    "feedback", "/comment", "comment.", "login", "signin", "sign-in",
    "subscribe", "newsletter", "/auth", "/account", "webcal",
    "javascript", "/advert", "/promo",
)

# 日期文本特征（用于给列表容器打分；真正的日期解析在 normalize.py）。
_DATE_PATTERN = re.compile(
    r"(?:\d{4}[-/年.]\s*\d{1,2}[-/月.]\s*\d{1,2}|\d{1,2}[-/月.]\s*\d{1,2}[-/日,.]\s*\d{4}"
    r"|\d+\s*(?:minutes?|mins?|hours?|hrs?|days?|weeks?)\s+ago"
    r"|\d+\s*(?:分钟|分|小时|天|周)前)",
    re.IGNORECASE,
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_CLASS_RE = re.compile(r"[\w-]+")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


@dataclass
class DiscoveryResult:
    """一次列表发现的结果。stats 由 agent 维护，这里只带方法/页面/feed/警告。"""

    items: list[NewsItem] = field(default_factory=list)
    method: str = ""  # profile | feed | jsonld | heuristic | mixed
    pages: list[str] = field(default_factory=list)
    feeds: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _emit(on_event: Callable[[str, str], None] | None, message: str, phase: str = "discovering") -> None:
    if not on_event:
        return
    try:
        on_event(str(message), phase)
    except Exception:  # noqa: BLE001 - 进度回调失败不影响发现流程
        return


def _warn(result: DiscoveryResult | None, message: str) -> None:
    """记录发现过程中的降级/失败信息（去重、限长）。"""
    if result is None:
        return
    text = str(message or "").strip()[:300]
    if text and text not in result.warnings and len(result.warnings) < 40:
        result.warnings.append(text)


def _trunc(value: Any, limit: int = 200) -> str:
    text = str(value if value is not None else "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _clean(value: Any) -> str:
    return dom.clean_ws(str(value if value is not None else ""))


def _strip_html(value: str) -> str:
    if not value:
        return ""
    return dom.clean_ws(_HTML_TAG_RE.sub(" ", str(value)))


def _url_key(url: str) -> str:
    """去追踪参数 + 去 fragment + 去尾斜杠的 URL 去重键。"""
    cleaned = strip_tracking(str(url or "").strip())
    if not cleaned:
        return ""
    parts = urlsplit(cleaned)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return cleaned
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _local_tag(element: Any) -> str:
    tag = getattr(element, "tag", "")
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _class_tokens(element: Any) -> list[str]:
    raw = ""
    try:
        raw = element.get("class") or ""
    except (AttributeError, TypeError):
        raw = ""
    tokens: list[str] = []
    for token in _CLASS_RE.findall(raw.lower()):
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def _element_marker(element: Any) -> str:
    """返回元素的 class/id 拼接文本，用于识别导航/页脚等噪声容器。"""
    try:
        raw = f"{element.get('class') or ''} {element.get('id') or ''}".lower()
    except (AttributeError, TypeError):
        return ""
    return raw


def _iter_ancestors(element: Any, limit: int = 8) -> list[Any]:
    ancestors: list[Any] = []
    node = element
    for _ in range(limit):
        try:
            node = node.getparent()
        except AttributeError:
            break
        if node is None:
            break
        ancestors.append(node)
    return ancestors


def _discovery_settings(settings: dict | None) -> dict[str, Any]:
    base: dict[str, Any] = {
        "feed_suffixes": list(DEFAULT_FEED_SUFFIXES),
        "max_feeds": 5,
        "anchor_min_text": 12,
        "max_scroll": 0,
    }
    raw = (settings or {}).get("discovery") or {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            base[key] = value
    return base


def _fetch_options(profile: SiteProfile | None, discovery: dict[str, Any]) -> dict[str, Any]:
    """合并站点画像的 fetch 选项与全局 discovery 配置。"""
    encoding = ""
    wait_selector = ""
    browser: bool | None = None
    scroll = 0
    try:
        scroll = int(discovery.get("max_scroll") or 0)
    except (TypeError, ValueError):
        scroll = 0
    if profile is not None:
        raw_encoding = str(profile.selector("fetch", "encoding") or "")
        encoding = "" if raw_encoding.lower() in ("", "auto") else raw_encoding
        wait_selector = str(profile.selector("fetch", "wait_selector") or "")
        mode = str(profile.selector("fetch", "mode") or "").lower()
        if mode == "always":
            browser = True
        elif mode == "never":
            browser = False
        try:
            scroll = max(scroll, int(profile.discovery.get("max_scroll") or 0))
        except (TypeError, ValueError):
            pass
    return {
        "encoding": encoding,
        "wait_selector": wait_selector,
        "scroll": max(0, scroll),
        "browser": browser,
    }


# ---------------------------------------------------------------------------
# 文章 URL 判断
# ---------------------------------------------------------------------------


def _reason_not_article_root(url: str) -> bool:
    """根路径 / 频道首页 / 索引页判断：这类 URL 永远不该当文章候选。"""
    parts = urlsplit(str(url or "").strip())
    path = (parts.path or "/").lower()
    stripped = path.strip("/")
    if not stripped:
        return True  # https://example.com/ 或 https://example.com
    segments = [segment for segment in stripped.split("/") if segment]
    last = segments[-1]
    if last in ("index.html", "index.htm", "index.shtml", "index.php", "default.aspx", "default.html"):
        return True
    if len(segments) == 1 and segments[0] in _CHANNEL_SEGMENTS:
        return True  # /news/、/world/、/latest/ 等频道首页
    return False


def _hard_reject_url(url: str) -> bool:
    """与站点无关的硬性排除：根路径/频道首页、明显非正文扩展名。"""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return True
    if _reason_not_article_root(url):
        return True
    path = (parts.path or "/").lower()
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if "." in last:
        extension = last.rsplit(".", 1)[-1]
        if extension in _BAD_EXTENSIONS:
            return True
    return False


def looks_like_article_url(url: str, config: CrawlConfig | None = None) -> bool:
    """判断一个 URL 是否像新闻文章页（用于过滤导航/标签/作者/登录等链接）。

    保持宽松：新闻站路径形态差异很大，只在命中明显特征时拒绝；
    站点画像或 include_patterns 可覆盖。
    """
    if not isinstance(url, str) or not url.strip():
        return False
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    path = (parts.path or "/").lower()
    query = parts.query or ""

    exclude_patterns = list(getattr(config, "exclude_patterns", None) or [])
    include_patterns = list(getattr(config, "include_patterns", None) or [])
    if exclude_patterns and url_matches_any(url, exclude_patterns):
        return False
    if include_patterns and url_matches_any(url, include_patterns):
        return True
    if _hard_reject_url(url):
        return False

    segments = [segment for segment in path.split("/") if segment]
    for segment in segments:
        base_segment = segment.rsplit(".", 1)[0] if "." in segment else segment
        if segment in _BAD_SEGMENTS or base_segment in _BAD_SEGMENTS:
            return False
    if not segments:
        return False

    # 跳转/中转端点不是文章本身（RSS 常见 redirect.php?url=... 会 302 到首页）：
    # 只有“路径是跳转语义 + 查询带跳转目标参数”同时成立才拒绝，避免误杀正常文章 query。
    if re.search(
        r"(?:^|/)(?:redirect|redir|jump|goto|out|outbound|away|link)(?:.[a-z]+)?/?$", path
    ) and re.search(
        r"(?:^|&)(?:url|u|to|target|link|dest|destination|redirect)=", query, re.IGNORECASE
    ):
        return False

    # 查询参数里的文章 id（WordPress / CMS 常见）
    if re.search(r"(?:^|&)(?:p|id|aid|article_?id|news_?id|doc_?id|story_?id)=\d+", query, re.I):
        return True

    path_no_ext = path
    extension = ""
    last_segment = segments[-1]
    if "." in last_segment:
        extension = last_segment.rsplit(".", 1)[-1]
        path_no_ext = path[: path.rfind(".")]
    digits = sum(1 for char in path_no_ext if char.isdigit())
    has_html_ext = extension in _ARTICLE_EXTENSIONS
    has_date = bool(
        re.search(r"/(?:19|20)\d{2}[-/]\d{1,2}(?:[-/]\d{1,2})?(?:/|$)", path_no_ext)
        or re.search(r"(?:19|20)\d{6}", path_no_ext)
        or re.search(r"/(?:19|20)\d{2}/(?:0?[1-9]|1[0-2])/", path_no_ext)
    )
    slug = segments[-1]
    if has_html_ext or has_date or digits >= 4:
        return True
    if len(segments) == 1:
        return False
    if slug.count("-") >= 2 or slug.count("_") >= 2:
        return True
    if len(segments) >= 3 and len(slug) >= 12:
        return True
    return False


# ---------------------------------------------------------------------------
# RSS / Atom
# ---------------------------------------------------------------------------


def _feed_children(element: Any, *names: str) -> list[Any]:
    wanted = {name.lower() for name in names}
    if element is None:
        return []
    return [child for child in element if _local_tag(child) in wanted]


def _feed_child(element: Any, *names: str) -> Any | None:
    children = _feed_children(element, *names)
    return children[0] if children else None


def _feed_text(element: Any) -> str:
    if element is None:
        return ""
    try:
        return _clean("".join(element.itertext()))
    except (TypeError, AttributeError):
        return ""


def _feed_attr(element: Any, name: str) -> str:
    """按本地名读取属性，兼容 media:content 之类带命名空间的属性。"""
    if element is None:
        return ""
    try:
        attributes = element.attrib
    except AttributeError:
        return ""
    for key, value in attributes.items():
        if str(key).rsplit("}", 1)[-1].lower() == name.lower():
            return str(value or "").strip()
    return ""


def _feed_entry_to_item(entry: Any, base_url: str) -> NewsItem | None:
    """把 RSS item / Atom entry / RDF item 归一为一个 NewsItem。"""
    title = _feed_text(_feed_child(entry, "title"))
    link = ""
    for link_element in _feed_children(entry, "link"):
        rel = _feed_attr(link_element, "rel").lower()
        href = _feed_attr(link_element, "href")
        if href and rel in ("", "alternate"):
            link = href
            break
        if not link:
            link = _feed_text(link_element)
    if not link:
        guid = _feed_text(_feed_child(entry, "guid")) or _feed_attr(_feed_child(entry, "guid"), "ispermalink")
        if guid.startswith("http"):
            link = guid
    if not link:
        link = _feed_text(_feed_child(entry, "id")) or _feed_attr(entry, "about")
    url = absolute_url(base_url, link) if link else ""
    if not url and str(link).startswith("http"):
        url = str(link).strip()
    if not url:
        return None

    published = ""
    for name in ("pubdate", "published", "updated", "issued", "created", "date"):
        element = _feed_child(entry, name)
        if element is not None:
            value = _feed_text(element) or _feed_attr(element, "term")
            if value:
                published = value
                break
    category = ""
    for element in _feed_children(entry, "category"):
        value = _feed_text(element) or _feed_attr(element, "term")
        if value:
            category = value
            break
    image = ""
    for name in ("enclosure", "content", "thumbnail", "image"):
        for element in _feed_children(entry, name):
            source = _feed_attr(element, "url") or _feed_attr(element, "href") or _feed_attr(element, "src")
            if source:
                image = source
                break
        if image:
            break
    if not title:
        summary = ""
        for name in ("description", "summary", "encoded"):
            summary = summary or _feed_text(_feed_child(entry, name))
        title = _strip_html(summary)[:160]
    return NewsItem(
        url=url,
        title=_trunc(title, 300),
        published_raw=_trunc(published, 100),
        section=_trunc(category, 80),
        image=image,
        method="feed",
    )


def extract_feed_items(xml_text: str, base_url: str = "", limit: int = 0) -> list[NewsItem]:
    """解析 RSS2 / Atom / RDF(RSS1.0) / JSON Feed 文本，返回 NewsItem 列表。"""
    items: list[NewsItem] = []
    text = str(xml_text or "").lstrip("\ufeff \r\n\t")
    if not text:
        return items
    if text.startswith("{"):
        return _extract_json_feed(text, base_url, limit)
    root = None
    candidates = [text]
    if ">" in text:
        candidates.append(text[: text.rfind(">") + 1])
    for candidate in candidates:
        try:
            root = ET.fromstring(candidate)
            break
        except ET.ParseError:
            root = None
    if root is None:
        return items
    root_name = _local_tag(root)
    entries: list[Any] = []
    if root_name == "rss":
        channel = _feed_child(root, "channel")
        entries = _feed_children(channel, "item") if channel is not None else []
    elif root_name == "feed":
        entries = _feed_children(root, "entry")
    elif root_name in ("rdf", "rss1"):
        entries = _feed_children(root, "item")
    else:
        entries = _feed_children(root, "item") or _feed_children(root, "entry")
    for entry in entries:
        item = _feed_entry_to_item(entry, base_url)
        if item is not None:
            items.append(item)
        if limit and len(items) >= int(limit):
            break
    return items


def _extract_json_feed(text: str, base_url: str, limit: int = 0) -> list[NewsItem]:
    """兼容 JSON Feed（少量站点使用），解析失败返回空列表。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    entries = data.get("items") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    items: list[NewsItem] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_url = entry.get("url") or entry.get("external_url") or entry.get("id") or ""
        url = absolute_url(base_url, str(raw_url)) if raw_url else ""
        if not url:
            continue
        tags = entry.get("tags") if isinstance(entry.get("tags"), list) else []
        image = entry.get("image") or entry.get("banner_image") or ""
        items.append(
            NewsItem(
                url=url,
                title=_trunc(_clean(entry.get("title") or ""), 300),
                published_raw=_trunc(
                    str(entry.get("date_published") or entry.get("date_modified") or ""), 100
                ),
                section=_trunc(_clean(tags[0] if tags else ""), 80),
                image=str(image or ""),
                method="feed",
            )
        )
        if limit and len(items) >= int(limit):
            break
    return items


def _is_feed_text(text: str) -> bool:
    """粗略判断响应文本是不是 feed（真正的解析仍交给 extract_feed_items）。"""
    sample = str(text or "")[:4000].lstrip("\ufeff \r\n\t").lower()
    if not sample:
        return False
    if sample.startswith("{"):
        return '"items"' in sample and '"title"' in sample
    if not sample.startswith("<"):
        return False
    return any(
        marker in sample
        for marker in ("<rss", "<feed", "<rdf:rdf", "rdf-syntax-ns", "<channel", "<item>")
    )


def discover_feeds(
    fetcher: Fetcher,
    page_url: str,
    config: CrawlConfig | None = None,
    tree: etree._Element | None = None,
    profile: SiteProfile | None = None,
    settings: dict | None = None,
    probe: bool = True,
) -> list[str]:
    """发现页面可用的 RSS/Atom 地址：画像配置 + 页面 link 声明 + 常见后缀探测。"""
    discovery = _discovery_settings(settings)
    try:
        max_feeds = max(1, int(discovery.get("max_feeds") or 5))
    except (TypeError, ValueError):
        max_feeds = 5
    candidates: list[str] = []

    def push(value: str, base: str = page_url) -> None:
        raw = str(value or "").strip()
        if not raw:
            return
        url = absolute_url(base, raw)
        if not url:
            return
        lowered = url.lower()
        if any(marker in lowered for marker in _FEED_NOISE_MARKERS):
            return
        # 注意：feed 地址本身就带 .xml/.rss 扩展名，不能用 _hard_reject_url 过滤；
        # 这里只排除首页（feed 不可能是站点根路径）。
        if not urlsplit(url).path.strip("/"):
            return
        key = _url_key(url)
        if not key or any(_url_key(existing) == key for existing in candidates):
            return
        candidates.append(url)

    if profile is not None:
        configured = profile.discovery.get("feeds") or []
        if isinstance(configured, str):
            configured = [configured]
        for raw in configured:
            if isinstance(raw, str):
                push(raw)
    if tree is not None:
        for link in tree.xpath("//link[@href]"):
            rel = (link.get("rel") or "").lower()
            link_type = (link.get("type") or "").lower()
            if "alternate" in rel and any(marker in link_type for marker in ("rss", "atom", "xml")):
                push(link.get("href") or "")
        for anchor in tree.xpath("//a[@href]"):
            href = anchor.get("href") or ""
            lowered = href.lower()
            if any(
                marker in lowered
                for marker in (".rss", ".atom", "/rss", "/feed", "/feeds/", "feed.xml", "rss.xml")
            ):
                push(href)

    feeds = candidates[:max_feeds]
    # 页面/画像已经声明了 feed 就不再探测后缀：既快又礼貌（避免无意义的 404 请求）。
    if not probe or feeds or len(feeds) >= max_feeds:
        return feeds

    suffixes = discovery.get("feed_suffixes") or list(DEFAULT_FEED_SUFFIXES)
    root = site_root(page_url).rstrip("/")
    try:
        probe_limit = int(discovery.get("feed_probe_limit") or 3)
    except (TypeError, ValueError):
        probe_limit = 3
    # 后缀探测只尝试前 N 个（默认 3），且一旦拿到有效 feed 立即停止：
    # 避免对 /feed /rss /rss.xml ... 逐个发请求（对站点不礼貌、也拖慢抓取）。
    probe_limit = max(0, min(probe_limit, max_feeds))
    attempted = 0
    for suffix in suffixes:
        if attempted >= probe_limit or len(feeds) >= max_feeds:
            break
        suffix_text = str(suffix or "").strip()
        if not suffix_text:
            continue
        candidate = root + (suffix_text if suffix_text.startswith("/") else "/" + suffix_text)
        if any(_url_key(existing) == _url_key(candidate) for existing in feeds):
            continue
        attempted += 1
        try:
            # feed 是 XML/JSON，不需要浏览器渲染；探测失败（404/robots/网络）静默跳过。
            fetched = fetcher.get(candidate, browser=False)
        except Exception:  # noqa: BLE001 - 探测失败（robots/网络）属于正常降级
            continue
        if not fetched.ok or not _is_feed_text(fetched.text):
            continue
        feeds.append(fetched.final_url or candidate)
        break
    return feeds[:max_feeds]


# ---------------------------------------------------------------------------
# 站点画像
# ---------------------------------------------------------------------------


def _node_href(node: Any, base_url: str) -> str:
    raw = ""
    if _local_tag(node) == "a":
        raw = node.get("href") or ""
    else:
        hrefs = node.xpath(".//a[@href]/@href")
        raw = str(hrefs[0]) if hrefs else (node.get("href") or "")
    return absolute_url(base_url, raw) if raw else ""


def _first_heading_text(node: Any) -> str:
    """在容器内找标题文本（h1-h4），找不到返回空串。"""
    try:
        headings = node.xpath(".//h1 | .//h2 | .//h3 | .//h4")
    except (AttributeError, TypeError):
        return ""
    for heading in headings:
        text = dom.node_text(heading)
        if text:
            return text
    return ""


def _node_image(node: Any, base_url: str) -> str:
    try:
        sources = node.xpath(".//img[@src]/@src")
    except (AttributeError, TypeError):
        return ""
    if not sources:
        return ""
    return absolute_url(base_url, str(sources[0])) or ""


def _candidate_allowed_url(
    url: str,
    page_url: str = "",
    config: CrawlConfig | None = None,
    profile: SiteProfile | None = None,
) -> bool:
    """feed / JSON-LD 条目的统一过滤：根路径/频道首页/非文章形态一律丢弃。

    站点画像 include 或 config.include_patterns 命中时放行（与启发式一致）。
    注意：这里不做严格同站限制——BBC 等站的 feed 域名（feeds.bbci.co.uk）与
    文章域名（bbc.co.uk）不同，严格同站会误杀；首页/栏目页由 _hard_reject_url
    与 looks_like_article_url 过滤。
    """
    if not url or _hard_reject_url(url):
        return False
    if profile is not None:
        includes = list(profile.include or [])
        if includes and url_matches_any(url, includes):
            return True
    return looks_like_article_url(url, config)


def _profile_url_allowed(url: str, profile: SiteProfile, config: CrawlConfig | None) -> bool:
    if _hard_reject_url(url):
        return False
    excludes = list(profile.exclude or []) + list(getattr(config, "exclude_patterns", None) or [])
    if excludes and url_matches_any(url, excludes):
        return False
    includes = list(profile.include or [])
    if includes and not url_matches_any(url, includes):
        return False
    config_includes = list(getattr(config, "include_patterns", None) or [])
    if config_includes and not url_matches_any(url, config_includes):
        return False
    return True


def _extract_profile_items(
    tree: etree._Element,
    page_url: str,
    profile: SiteProfile,
    config: CrawlConfig | None,
) -> list[NewsItem]:
    """按站点画像 list.item 选择器抽取条目（含 title/date/section/image 附加字段）。"""
    selector = profile.selector("list", "item")
    if not selector:
        return []
    items: list[NewsItem] = []
    base_selector = selector.split("::")[0].strip()
    nodes = dom.select_nodes(tree, base_selector)
    if not nodes:
        # 选择器只能取值（例如 a[href]::attr(href)）时退化为纯 URL 列表
        for href in dom.select_values(tree, selector):
            url = absolute_url(page_url, href)
            if not url or not _profile_url_allowed(url, profile, config):
                continue
            items.append(NewsItem(url=url, list_url=page_url, method="profile"))
        return items

    title_selector = profile.selector("list", "item_title")
    date_selector = profile.selector("list", "item_date")
    section_selector = profile.selector("list", "item_section")
    image_selector = profile.selector("list", "item_image")
    for node in nodes:
        url = _node_href(node, page_url)
        if not url or not _profile_url_allowed(url, profile, config):
            continue
        title = dom.first_value(node, title_selector) if title_selector else ""
        if not title:
            title = _first_heading_text(node) or dom.node_text(node)
        date = dom.first_value(node, date_selector) if date_selector else ""
        if not date:
            date = _nearby_date(node)
        section = dom.first_value(node, section_selector) if section_selector else ""
        image = dom.first_value(node, image_selector) if image_selector else ""
        if not image:
            image = _node_image(node, page_url)
        elif image:
            image = absolute_url(page_url, image) or image
        items.append(
            NewsItem(
                url=url,
                title=_trunc(title, 300),
                published_raw=_trunc(date, 100),
                section=_trunc(section, 80),
                image=image,
                list_url=page_url,
                method="profile",
            )
        )
    return items


def _profile_next_url(tree: etree._Element, current_url: str, profile: SiteProfile, page_number: int) -> str:
    """按画像 pagination.next / url_template 计算下一页地址。"""
    pagination = profile.list.get("pagination") or {}
    next_selector = str(pagination.get("next") or "").strip()
    if next_selector:
        for value in dom.select_values(tree, next_selector):
            candidate = absolute_url(current_url, value)
            if candidate:
                return candidate
    template = str(pagination.get("url_template") or "").strip()
    if template and page_number > 0:
        candidate = template.replace("{page}", str(page_number)).replace("{p}", str(page_number))
        resolved = absolute_url(current_url, candidate)
        if resolved:
            return resolved
    return ""


# ---------------------------------------------------------------------------
# JSON-LD
# ---------------------------------------------------------------------------


def _type_set(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, str):
        names.add(value.strip().lower())
    elif isinstance(value, list):
        for entry in value:
            names.update(_type_set(entry))
    return names


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _json_ld_url(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("url", "@id", "contentUrl", "sameAs"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""
    if isinstance(value, list):
        for entry in value:
            candidate = _json_ld_url(entry)
            if candidate:
                return candidate
    return ""


def _jsonld_items(tree: etree._Element, page_url: str, config: CrawlConfig | None = None) -> list[NewsItem]:
    """从 ItemList / CollectionPage / WebPage.mainEntity / @graph 文章对象提取条目。"""
    objects = dom.json_ld_objects(tree)
    if not objects:
        return []
    items: list[NewsItem] = []
    page_key = _url_key(page_url)

    def add(url_value: Any, title: Any = "", date: Any = "", section: Any = "", image: Any = "") -> None:
        raw = _json_ld_url(url_value)
        if not raw:
            return
        url = absolute_url(page_url, raw) or raw
        key = _url_key(url)
        if not key or key == page_key:
            return
        if not _candidate_allowed_url(url, page_url, config):
            return
        items.append(
            NewsItem(
                url=url,
                title=_trunc(dom.json_ld_text(title), 300),
                published_raw=_trunc(dom.json_ld_text(date), 100),
                section=_trunc(dom.json_ld_text(section), 80),
                image=_trunc(_json_ld_url(image), 500),
                list_url=page_url,
                method="jsonld",
            )
        )

    def add_object(obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        url = obj.get("url") or obj.get("@id") or obj.get("mainEntityOfPage")
        add(
            url,
            obj.get("headline") or obj.get("name"),
            obj.get("datePublished") or obj.get("dateCreated") or obj.get("uploadDate"),
            obj.get("articleSection") or obj.get("genre"),
            obj.get("image") or obj.get("thumbnailUrl"),
        )

    def walk_item_list(item_list: Any, depth: int = 0) -> None:
        if not isinstance(item_list, dict) or depth > 3:
            return
        for element in _as_list(item_list.get("itemListElement")):
            if isinstance(element, str):
                add(element)
                continue
            if not isinstance(element, dict):
                continue
            item = element.get("item")
            if isinstance(item, dict):
                if "itemlist" in _type_set(item.get("@type")):
                    walk_item_list(item, depth + 1)
                else:
                    add_object(item)
            elif isinstance(item, list):
                for entry in item:
                    if isinstance(entry, dict):
                        add_object(entry)
                    elif isinstance(entry, str):
                        add(entry)
            elif isinstance(item, str):
                add(item, element.get("name"))
            else:
                add(
                    element.get("url") or element.get("@id"),
                    element.get("name") or element.get("headline"),
                    element.get("datePublished"),
                    element.get("articleSection"),
                    element.get("image"),
                )

    article_types = {
        "newsarticle", "blogposting", "article", "reportagenewsarticle",
        "analysisnewsarticle", "opinionnewsarticle", "techarticle",
    }
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        types = _type_set(obj.get("@type"))
        if "itemlist" in types:
            walk_item_list(obj)
        elif types & {"collectionpage", "webpage", "profilepage", "searchresultspage"}:
            main = obj.get("mainEntity")
            if isinstance(main, dict):
                if "itemlist" in _type_set(main.get("@type")):
                    walk_item_list(main)
                else:
                    add_object(main)
            for part in _as_list(obj.get("hasPart")):
                add_object(part)
        elif types & article_types:
            add_object(obj)
    return items


# ---------------------------------------------------------------------------
# 链接聚类启发式
# ---------------------------------------------------------------------------


def _in_noise_context(anchor: Any) -> bool:
    """跳过 header/footer/nav/aside 与导航类容器里的链接。"""
    for ancestor in _iter_ancestors(anchor, 8):
        tag = _local_tag(ancestor)
        if tag in ("nav", "footer", "header", "aside"):
            return True
        marker_text = _element_marker(ancestor)
        if marker_text and any(marker in marker_text for marker in _NAV_MARKERS):
            return True
    return False


def _safe_xpath(element: Any, expression: str) -> list[Any]:
    """安全 xpath：元素类型异常时返回空列表，不让发现流程崩溃。"""
    try:
        return element.xpath(expression)
    except (AttributeError, TypeError, etree.XPathError):
        return []


def _anchor_title(anchor: Any) -> str:
    # 列表卡片常见 <a><span class="index">1</span><h2>标题</h2></a>：
    # 优先取卡片内标题标签文本，避免把序号/辅助文本拼进标题。
    for heading in _safe_xpath(anchor, ".//h1 | .//h2 | .//h3 | .//h4"):
        text = dom.node_text(heading)
        if len(text) >= 2:
            return _trunc(text, 300)
    title = dom.node_text(anchor)
    if len(title) >= 2:
        return _trunc(title, 300)
    attribute = (anchor.get("title") or "").strip()
    if not attribute:
        try:
            alts = anchor.xpath(".//img[@alt]/@alt")
        except (AttributeError, TypeError):
            alts = []
        attribute = str(alts[0]).strip() if alts else ""
    return _trunc(attribute, 300)


def _in_heading(anchor: Any) -> bool:
    node = anchor
    for _ in range(3):
        try:
            node = node.getparent()
        except AttributeError:
            return False
        if node is None:
            return False
        if _local_tag(node) in ("h1", "h2", "h3", "h4", "h5", "h6"):
            return True
    return False


def _nearby_date(node: Any) -> str:
    """在链接附近（最近三级容器文本）寻找日期特征文本。"""
    current = node
    for _ in range(3):
        try:
            current = current.getparent()
        except AttributeError:
            break
        if current is None:
            break
        try:
            text = dom.node_text(current)
        except (AttributeError, TypeError):
            text = ""
        if not text:
            continue
        match = _DATE_PATTERN.search(text)
        if match:
            return _trunc(match.group(0), 60)
    return ""


def _element_sig(element: Any) -> str:
    """元素自身的重复结构签名：tag.class1.class2#id。"""
    tag = _local_tag(element)
    if not tag:
        return ""
    classes = _class_tokens(element)[:4]
    ident = ""
    try:
        ident = (element.get("id") or "").strip()
    except (AttributeError, TypeError):
        ident = ""
    signature = tag
    if classes:
        signature += "." + ".".join(classes)
    if ident and re.fullmatch(r"[A-Za-z][\w-]*", ident):
        signature += "#" + ident
    return signature


def _is_meaningful_ancestor(element: Any) -> bool:
    tag = _local_tag(element)
    if not tag:
        return False
    if _class_tokens(element):
        return True
    try:
        if (element.get("id") or "").strip():
            return True
    except (AttributeError, TypeError):
        pass
    return tag in ("article", "li", "section", "h2", "h3", "h4", "main", "tr", "dl", "ul", "ol")


def _css_hint(element: Any) -> str:
    """把祖先元素转成可直接在 dom 选择器里使用的 CSS 片段。"""
    tag = _local_tag(element) or "*"
    try:
        ident = (element.get("id") or "").strip()
    except (AttributeError, TypeError):
        ident = ""
    classes = [token for token in _class_tokens(element) if re.fullmatch(r"[A-Za-z_][\w-]*", token)]
    if ident and re.fullmatch(r"[A-Za-z][\w-]*", ident):
        return f"{tag}#{ident}"
    if classes:
        return tag + "." + ".".join(classes[:2])
    return tag


def _group_context(anchor: Any) -> tuple[str, str, str]:
    """返回 (分组键, CSS 提示, 祖先上下文)。

    分组键形如 ul.list > li，用“最近的有意义祖先 + 其父容器签名”表达
    重复子结构，避免把导航列表与正文列表混为一组。
    """
    ancestors = _iter_ancestors(anchor, 8)
    chain_text = " ".join(_element_marker(element) for element in ancestors)
    chosen: Any = None
    chosen_parent: Any = None
    for index, ancestor in enumerate(ancestors):
        if _is_meaningful_ancestor(ancestor):
            chosen = ancestor
            chosen_parent = ancestors[index + 1] if index + 1 < len(ancestors) else None
            break
    if chosen is None:
        return "", "a", chain_text
    own_sig = _element_sig(chosen) or (_local_tag(chosen) or "a")
    parent_sig = _element_sig(chosen_parent) if chosen_parent is not None else ""
    key = f"{parent_sig} > {own_sig}" if parent_sig else own_sig
    selector = f"{_css_hint(chosen)} a"
    if parent_sig:
        parent_hint = _css_hint(chosen_parent)
        if parent_hint not in ("body", "html"):
            selector = f"{parent_hint} > {_css_hint(chosen)} a"
    return key, selector, chain_text


def _collect_anchor_records(
    tree: etree._Element,
    page_url: str,
    config: CrawlConfig | None,
) -> list[dict[str, Any]]:
    """收集同站候选链接并计算聚类所需的上下文特征。"""
    records: list[dict[str, Any]] = []
    page_key = _url_key(page_url)
    domain = domain_from_url(page_url)
    for anchor in tree.xpath("//a[@href]"):
        href = anchor.get("href") or ""
        url = absolute_url(page_url, href)
        if not url or _url_key(url) == page_key:
            continue
        if domain and not is_same_site(url, domain):
            continue
        if not looks_like_article_url(url, config):
            continue
        if _in_noise_context(anchor):
            continue
        title = _anchor_title(anchor)
        if not title:
            continue
        key, selector, context = _group_context(anchor)
        records.append(
            {
                "url": url,
                "title": title,
                "key": _url_key(url),
                "group": key,
                "selector": selector,
                "context": context,
                "in_heading": _in_heading(anchor),
                "date": _nearby_date(anchor),
            }
        )
    return records


def _group_records(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for record in records:
        group_key = record.get("group") or "a"
        group = groups.get(group_key)
        if group is None:
            group = {"key": group_key, "selector": record.get("selector") or "a", "records": []}
            groups[group_key] = group
        group["records"].append(record)
    return groups


def _score_group(group: dict[str, Any]) -> float:
    """给一个候选容器打分：重复数量 + 标题/日期/标题标签上下文 - 导航惩罚。"""
    unique: dict[str, dict[str, Any]] = {}
    for record in group.get("records") or []:
        unique.setdefault(record.get("key") or record.get("url") or "", record)
    members = list(unique.values())
    count = len(members)
    if count < 3:
        return -1.0
    titles = [len(record.get("title") or "") for record in members if record.get("title")]
    average_title = sum(titles) / max(1, len(titles))
    score = float(min(count, 30))
    if 10 <= average_title <= 120:
        score += 4.0
    elif average_title >= 6:
        score += 1.0
    heading_ratio = sum(1 for record in members if record.get("in_heading")) / count
    score += min(4.0, heading_ratio * 6.0)
    date_ratio = sum(1 for record in members if record.get("date")) / count
    score += min(4.0, date_ratio * 6.0)
    context = str(members[0].get("context") or "")
    if any(marker in context for marker in _NAV_MARKERS):
        score -= 15.0
    if count > 80:
        score -= 12.0
    if ">" not in str(group.get("key") or ""):
        score -= 3.0
    return score


def _heuristic_items(
    tree: etree._Element,
    page_url: str,
    config: CrawlConfig | None,
    discovery: dict[str, Any],
    on_event: Callable[[str, str], None] | None = None,
) -> list[NewsItem]:
    """链接聚类启发式：选出最像新闻列表的容器并取其中链接。"""
    records = _collect_anchor_records(tree, page_url, config)
    if not records:
        _emit(on_event, "启发式：未发现符合文章规则的链接", "discovering")
        return []
    groups = _group_records(records)
    scored: list[tuple[float, dict[str, Any]]] = []
    for group in groups.values():
        score = _score_group(group)
        if score > 0:
            scored.append((score, group))
    scored.sort(key=lambda entry: entry[0], reverse=True)
    chosen: dict[str, Any] | None = None
    for score, group in scored:
        unique_keys = {record.get("key") for record in group["records"]}
        if score >= 6.0 and len(unique_keys) >= 3:
            chosen = group
            break
    source = chosen["records"] if chosen else records
    if chosen:
        count = len({record.get("key") for record in chosen["records"]})
        _emit(on_event, f"链接聚类命中 {chosen.get('selector')}（{count} 条）", "discovering")
    else:
        _emit(on_event, "链接聚类未命中稳定容器，回退为全页文章链接", "discovering")
    items: list[NewsItem] = []
    seen: set[str] = set()
    for record in source:
        key = record.get("key") or ""
        if not key or key in seen:
            continue
        seen.add(key)
        items.append(
            NewsItem(
                url=record["url"],
                title=_trunc(record.get("title") or "", 300),
                published_raw=_trunc(record.get("date") or "", 100),
                list_url=page_url,
                method="heuristic",
            )
        )
    return items


def candidate_link_selectors(tree: etree._Element, min_count: int = 3) -> list[dict[str, Any]]:
    """给 Agent inspect 用的候选列表选择器（按链接数量排序的 CSS 提示）。"""
    if tree is None:
        return []
    groups: dict[str, dict[str, Any]] = {}
    for anchor in tree.xpath("//a[@href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        title = _anchor_title(anchor)
        if not title:
            continue
        key, selector, _context = _group_context(anchor)
        group_key = key or "a"
        group = groups.get(group_key)
        if group is None:
            group = {"selector": selector or "a", "count": 0, "sample_titles": [], "noise": True}
            groups[group_key] = group
        group["count"] += 1
        # 组内只要出现过非导航链接，就视为正文候选容器
        if group["noise"] and not _in_noise_context(anchor):
            group["noise"] = False
        if title and len(group["sample_titles"]) < 3:
            group["sample_titles"].append(_trunc(title, 80))
    threshold = max(1, int(min_count))
    entries: list[dict[str, Any]] = []
    for group in groups.values():
        if group["count"] < threshold:
            continue
        entries.append(
            {
                "selector": group["selector"],
                "count": group["count"],
                "sample_titles": group["sample_titles"],
                "_noise": bool(group["noise"]),
            }
        )
    # 优先推荐“非导航”容器（新闻列表往往在这里）；不足 3 组时回退全量。
    content_entries = [entry for entry in entries if not entry["_noise"]]
    pool = content_entries if len(content_entries) >= 3 else entries
    result = [
        {
            "selector": entry["selector"],
            "count": entry["count"],
            "sample_titles": entry["sample_titles"],
        }
        for entry in pool
    ]
    result.sort(key=lambda entry: entry["count"], reverse=True)
    return result[:8]


# ---------------------------------------------------------------------------
# 翻页
# ---------------------------------------------------------------------------


def _increment_page_url(url: str) -> str:
    """把 URL 里的页码参数/路径段 +1，得到下一页地址；识别不到返回空串。"""
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    changed = False
    new_pairs: list[tuple[str, str]] = []
    for key, value in pairs:
        lowered = key.lower()
        if lowered in ("page", "p", "pn", "paged", "pageindex", "page_index") and value.isdigit():
            number = int(value)
            if 0 <= number <= 200:
                new_pairs.append((key, str(number + 1)))
                changed = True
                continue
        new_pairs.append((key, value))
    if changed:
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(new_pairs), ""))
    for pattern in (r"(/page/)(\d+)(?=/|$)", r"(/p/)(\d+)(?=/|$)", r"(/pages/)(\d+)(?=/|$)"):
        new_path, count = re.subn(
            pattern,
            lambda match: match.group(1) + str(int(match.group(2)) + 1),
            parts.path,
            count=1,
        )
        if count:
            return urlunsplit((parts.scheme, parts.netloc, new_path, parts.query, ""))
    return ""


def _find_next_page(
    tree: etree._Element,
    page_url: str,
    visited: set[str],
) -> str:
    """按 rel=next → URL 页码模式 → 多语言 next 文本的顺序找下一页。"""
    domain = domain_from_url(page_url)
    rel_candidates: list[str] = []
    text_candidates: list[str] = []
    for node in tree.xpath("//a[@href] | //link[@href]"):
        raw = node.get("href") or ""
        candidate = absolute_url(page_url, raw)
        if not candidate:
            continue
        key = _url_key(candidate)
        if not key or key in visited:
            continue
        if domain and not is_same_site(candidate, domain):
            continue
        rel = (node.get("rel") or "").lower().split()
        if "next" in rel:
            rel_candidates.append(candidate)
            continue
        label = (node.get("aria-label") or "").strip().lower()
        text = (dom.node_text(node) or "").strip().lower()
        if len(text) > 40:
            continue
        joined = f"{label} {text}".strip()
        if joined and any(keyword in joined for keyword in _NEXT_KEYWORDS):
            text_candidates.append(candidate)
    if rel_candidates:
        return rel_candidates[0]
    pattern_url = _increment_page_url(page_url)
    if pattern_url and _url_key(pattern_url) not in visited:
        return pattern_url
    if text_candidates:
        return text_candidates[0]
    return ""


# ---------------------------------------------------------------------------
# 抓取辅助
# ---------------------------------------------------------------------------


def _fetch_page(
    fetcher: Fetcher,
    url: str,
    config: CrawlConfig,
    result: DiscoveryResult,
    on_event: Callable[[str, str], None] | None,
    options: dict[str, Any] | None = None,
) -> Any | None:
    """抓取页面并记录失败/robots 拒绝；失败返回 None。"""
    options = options or {}
    try:
        fetched = fetcher.get(
            url,
            browser=options.get("browser"),
            wait_selector=str(options.get("wait_selector") or ""),
            encoding=str(options.get("encoding") or ""),
            scroll=int(options.get("scroll") or 0),
        )
    except Exception as error:  # noqa: BLE001 - RobotsDenied/FetchError 记录为警告并降级
        _warn(result, f"页面抓取失败（{type(error).__name__}）：{url} - {error}")
        return None
    if not fetched.ok:
        _warn(
            result,
            f"页面抓取失败：{url}（HTTP {fetched.status}，{fetched.error or '无内容'}）",
        )
        return None
    if fetched.method == "browser":
        _emit(on_event, f"浏览器渲染完成：{fetched.final_url or url}", "discovering")
    return fetched


def _parse_tree(fetched: Any, result: DiscoveryResult) -> etree._Element | None:
    try:
        return dom.parse_html(fetched.text)
    except Exception as error:  # noqa: BLE001 - 解析失败只降级
        _warn(result, f"HTML 解析失败：{fetched.final_url or fetched.url} - {error}")
        return None


# ---------------------------------------------------------------------------
# 各发现策略
# ---------------------------------------------------------------------------


def _profile_discover(
    fetcher: Fetcher,
    page_url: str,
    tree: etree._Element,
    config: CrawlConfig,
    profile: SiteProfile,
    options: dict[str, Any],
    result: DiscoveryResult,
    add_items: Callable[[list[NewsItem], str, str], int],
    enough: Callable[[], bool],
    on_event: Callable[[str, str], None] | None,
) -> None:
    """站点画像发现：逐页抽取 list.item，按 pagination 规则翻页。"""
    pagination = profile.list.get("pagination") or {}
    try:
        profile_max = int(pagination.get("max_pages") or config.max_list_pages)
    except (TypeError, ValueError):
        profile_max = int(config.max_list_pages)
    max_pages = max(1, min(int(config.max_list_pages), profile_max))
    current_url = page_url
    current_tree = tree
    for page_index in range(max_pages):
        items = _extract_profile_items(current_tree, current_url, profile, config)
        added = add_items(items, "profile", current_url)
        _emit(on_event, f"站点画像第 {page_index + 1} 页：{len(items)} 条（新增 {added}）", "discovering")
        if enough():
            break
        next_url = _profile_next_url(current_tree, current_url, profile, page_index + 2)
        if not next_url:
            next_url = _find_next_page(current_tree, current_url, {_url_key(page_url)})
        if not next_url or _url_key(next_url) in {_url_key(page) for page in result.pages}:
            break
        fetched = _fetch_page(fetcher, next_url, config, result, on_event, options)
        if fetched is None:
            break
        fetched_tree = _parse_tree(fetched, result)
        if fetched_tree is None:
            break
        current_url = fetched.final_url or next_url
        current_tree = fetched_tree
        result.pages.append(current_url)


def _feed_discover(
    fetcher: Fetcher,
    page_url: str,
    tree: etree._Element | None,
    config: CrawlConfig,
    profile: SiteProfile | None,
    settings: dict | None,
    discovery: dict[str, Any],
    result: DiscoveryResult,
    add_items: Callable[[list[NewsItem], str, str], int],
    enough: Callable[[], bool],
    on_event: Callable[[str, str], None] | None,
) -> None:
    """RSS/Atom 发现：画像/页面声明/后缀探测，逐个解析并把条目加入结果。"""
    feeds = discover_feeds(fetcher, page_url, config, tree=tree, profile=profile, settings=settings)
    if not feeds:
        return
    try:
        max_feeds = max(1, int(discovery.get("max_feeds") or 5))
    except (TypeError, ValueError):
        max_feeds = 5
    failed_feeds = 0
    filtered_feeds = 0
    for feed_url in feeds:
        if len(result.feeds) >= max_feeds or enough():
            break
        if any(_url_key(existing) == _url_key(feed_url) for existing in result.feeds):
            continue
        fetched = _fetch_page(fetcher, feed_url, config, result, on_event, {"browser": False})
        if fetched is None:
            failed_feeds += 1
            continue
        raw_items = extract_feed_items(fetched.text, fetched.final_url or feed_url)
        # RSS 条目同样要过文章 URL 过滤：feed 里常混入首页/栏目页（如新浪首页）。
        items = [
            item
            for item in raw_items
            if _candidate_allowed_url(item.url, page_url, config, profile)
        ]
        if not items:
            if raw_items:
                filtered_feeds += 1
            else:
                failed_feeds += 1
            continue
        added = add_items(items, "feed", feed_url)
        result.feeds.append(feed_url)
        _emit(
            on_event,
            f"RSS/Atom 发现 {len(items)} 条（解析 {len(raw_items)} 条，新增 {added}）：{feed_url}",
            "discovering",
        )
    notes: list[str] = []
    if failed_feeds:
        notes.append(f"{failed_feeds} 个 feed 地址不可用")
    if filtered_feeds:
        notes.append(f"{filtered_feeds} 个 feed 条目均非文章链接")
    if notes:
        _warn(result, "；".join(notes) + "（已跳过）")


def _jsonld_discover(
    tree: etree._Element,
    page_url: str,
    config: CrawlConfig,
    add_items: Callable[[list[NewsItem], str, str], int],
    on_event: Callable[[str, str], None] | None,
) -> None:
    items = _jsonld_items(tree, page_url, config)
    if not items:
        return
    added = add_items(items, "jsonld", page_url)
    _emit(on_event, f"JSON-LD 发现 {len(items)} 条（新增 {added}）", "discovering")


def _paginate_discover(
    fetcher: Fetcher,
    page_url: str,
    tree: etree._Element,
    config: CrawlConfig,
    discovery: dict[str, Any],
    result: DiscoveryResult,
    add_items: Callable[[list[NewsItem], str, str], int],
    enough: Callable[[], bool],
    on_event: Callable[[str, str], None] | None,
) -> None:
    """通用翻页：对后续列表页继续做 JSON-LD + 链接聚类，最多 max_list_pages 页。"""
    max_pages = max(1, int(config.max_list_pages or 1))
    current_url = page_url
    current_tree = tree
    visited = {_url_key(url) for url in result.pages}
    while not enough() and len(result.pages) < max_pages:
        next_url = _find_next_page(current_tree, current_url, visited)
        if not next_url:
            break
        fetched = _fetch_page(fetcher, next_url, config, result, on_event, {})
        if fetched is None:
            break
        fetched_tree = _parse_tree(fetched, result)
        if fetched_tree is None:
            break
        current_url = fetched.final_url or next_url
        current_tree = fetched_tree
        visited.add(_url_key(current_url))
        result.pages.append(current_url)
        added = add_items(_jsonld_items(current_tree, current_url, config), "jsonld", current_url)
        added += add_items(
            _heuristic_items(current_tree, current_url, config, discovery, on_event),
            "heuristic",
            current_url,
        )
        _emit(on_event, f"翻页第 {len(result.pages)} 页：{current_url}（新增 {added} 条）", "discovering")
        if added == 0:
            break


# ---------------------------------------------------------------------------
# 对外主函数
# ---------------------------------------------------------------------------


def discover_news(
    fetcher: Fetcher,
    entry_url: str,
    config: CrawlConfig,
    profile: SiteProfile | None = None,
    settings: dict | None = None,
    on_event: Callable[[str, str], None] | None = None,
) -> DiscoveryResult:
    """发现新闻列表条目：画像 → RSS/Atom → JSON-LD → 链接聚类 → 翻页。"""
    discovery = _discovery_settings(settings)
    result = DiscoveryResult()
    method_counts: dict[str, int] = {}
    items_index: dict[str, NewsItem] = {}
    max_candidates = max(1, int(config.max_candidates or 100))
    target = max(8, min(int(config.max_articles or 10), 30))

    def add_items(new_items: list[NewsItem], method_name: str, page_url: str = "") -> int:
        added = 0
        for item in new_items:
            # 全局兜底：任何发现方式都不允许根路径/频道首页/非正文链接进入候选。
            if _hard_reject_url(item.url):
                continue
            key = _url_key(item.url)
            if not key or key in items_index:
                continue
            if not item.list_url:
                item.list_url = page_url
            if not item.method:
                item.method = method_name
            items_index[key] = item
            added += 1
        if added:
            method_counts[method_name] = method_counts.get(method_name, 0) + added
        return added

    def enough() -> bool:
        return len(items_index) >= target

    entry = str(entry_url or config.entry_url).strip()
    list_url = entry
    if profile is not None and profile.list.get("url"):
        override = absolute_url(entry, str(profile.list["url"]))
        if override:
            list_url = override
    options = _fetch_options(profile, discovery)

    first = _fetch_page(fetcher, list_url, config, result, on_event, options)
    if first is None:
        # 列表页被反爬/网络阻断时仍尝试 feed（常见：页面 406 但 RSS 可访问）。
        _feed_discover(
            fetcher, entry, None, config, profile, settings, discovery,
            result, add_items, enough, on_event,
        )
        result.items = list(items_index.values())[:max_candidates]
        result.method = "feed" if result.items else ""
        if not result.items:
            _warn(result, "列表页抓取失败且未发现可用 feed，请尝试浏览器模式或站点画像。")
        return result
    page_url = first.final_url or list_url
    result.pages.append(page_url)
    _emit(on_event, f"列表页已抓取：{page_url}（{first.method}）", "discovering")

    # 入口 URL 本身是 RSS/Atom：直接按 feed 处理
    if _is_feed_text(first.text):
        raw_feed_items = extract_feed_items(first.text, page_url)
        feed_items = [
            item
            for item in raw_feed_items
            if _candidate_allowed_url(item.url, page_url, config, profile)
        ]
        if len(feed_items) >= 2:
            add_items(feed_items, "feed", page_url)
            result.feeds.append(page_url)
            result.method = "feed"
            result.items = list(items_index.values())[:max_candidates]
            _emit(on_event, f"入口为 RSS/Atom，发现 {len(result.items)} 条", "discovering")
            return result

    tree = _parse_tree(first, result)
    if tree is None:
        result.items = list(items_index.values())[:max_candidates]
        return result

    # 1) 站点画像
    if profile is not None and profile.selector("list", "item"):
        _profile_discover(
            fetcher, page_url, tree, config, profile, options,
            result, add_items, enough, on_event,
        )
    # 2) RSS / Atom
    if not enough():
        _feed_discover(
            fetcher, page_url, tree, config, profile, settings, discovery,
            result, add_items, enough, on_event,
        )
    # 3) JSON-LD
    if not enough():
        _jsonld_discover(tree, page_url, config, add_items, on_event)
    # 4) 链接聚类启发式
    if not enough():
        heuristic = _heuristic_items(tree, page_url, config, discovery, on_event)
        added = add_items(heuristic, "heuristic", page_url)
        _emit(on_event, f"链接聚类发现 {len(heuristic)} 条（新增 {added}）", "discovering")
    # 5) 翻页（JSON-LD + 启发式继续）
    if not enough():
        _paginate_discover(
            fetcher, page_url, tree, config, discovery,
            result, add_items, enough, on_event,
        )

    result.items = list(items_index.values())[:max_candidates]
    if not method_counts:
        result.method = ""
    elif len(method_counts) == 1:
        result.method = next(iter(method_counts))
    else:
        result.method = "mixed"
    if not result.items:
        _warn(result, "未能发现文章链接：站点可能需要站点画像 / RSS / 浏览器模式。")
    _emit(
        on_event,
        f"列表发现完成：{len(result.items)} 条候选（{result.method or 'none'}）",
        "discovered",
    )
    return result


# ---------------------------------------------------------------------------
# probe_structure：给 Agent inspect_site 用的结构报告
# ---------------------------------------------------------------------------


def _pagination_hint(tree: etree._Element, page_url: str) -> str:
    next_url = _find_next_page(tree, page_url, set())
    if next_url:
        return _trunc(f"next: {next_url}", 200)
    pattern = _increment_page_url(page_url)
    if pattern:
        return _trunc(f"url_pattern: {pattern}", 200)
    return ""


def _probe_article_page(
    fetcher: Fetcher,
    article_url: str,
    config: CrawlConfig,
    profile: SiteProfile | None,
    options: dict[str, Any],
) -> dict[str, Any]:
    """抓取第一个候选文章页，探测 title/body/date 的候选选择器与样例值。"""
    probe: dict[str, Any] = {"url": article_url, "title": [], "body": [], "date": []}
    tmp = DiscoveryResult()
    fetched = _fetch_page(fetcher, article_url, config, tmp, None, options)
    if fetched is None:
        probe["error"] = _trunc(tmp.warnings[0] if tmp.warnings else "文章页抓取失败", 160)
        return probe
    try:
        tree = dom.parse_html(fetched.text)
    except Exception as error:  # noqa: BLE001 - 探测失败不影响整体报告
        probe["error"] = _trunc(f"文章页解析失败：{error}", 160)
        return probe

    title_selectors: list[str] = []
    body_selectors: list[str] = []
    date_selectors: list[str] = []
    if profile is not None:
        title_selectors.append(profile.selector("article", "title"))
        body_selectors.append(profile.selector("article", "body"))
        date_selectors.append(profile.selector("article", "date"))
    title_selectors += ["h1", "h1[itemprop=headline]", "meta[property=og:title]::attr(content)", "title"]
    body_selectors += [
        "[itemprop=articleBody]", "article", "[class*=article-body]",
        "[class*=story-body]", "[class*=entry-content]", "main",
    ]
    date_selectors += [
        "time[datetime]::attr(datetime)",
        "meta[property=article:published_time]::attr(content)",
        "meta[name=publishdate]::attr(content)",
        "[class*=date]",
        "[class*=time]",
        "time",
    ]

    for selector in [item for item in title_selectors if item]:
        value = dom.first_value(tree, selector)
        if not value:
            continue
        probe["title"].append({"selector": selector, "value": _trunc(value, 200)})
        if len(probe["title"]) >= 3:
            break
    json_ld = dom.first_json_ld(tree, ("NewsArticle", "Article", "BlogPosting", "ReportageNewsArticle"))
    headline = dom.json_ld_text(json_ld.get("headline")) if json_ld else ""
    if headline and len(probe["title"]) < 3:
        probe["title"].append({"selector": "json-ld:headline", "value": _trunc(headline, 200)})

    for selector in [item for item in body_selectors if item]:
        nodes = dom.select_nodes(tree, selector.split("::")[0].strip())
        if not nodes:
            continue
        text = dom.container_text(nodes[0])
        if len(text) < 60:
            continue
        probe["body"].append({"selector": selector, "chars": len(text), "sample": _trunc(text, 200)})
        if len(probe["body"]) >= 3:
            break
    article_body = dom.json_ld_text(json_ld.get("articleBody")) if json_ld else ""
    if article_body and len(probe["body"]) < 3:
        probe["body"].append(
            {"selector": "json-ld:articleBody", "chars": len(article_body), "sample": _trunc(article_body, 200)}
        )

    for selector in [item for item in date_selectors if item]:
        value = dom.first_value(tree, selector)
        if not value:
            continue
        probe["date"].append({"selector": selector, "value": _trunc(value, 200)})
        if len(probe["date"]) >= 3:
            break
    json_date = ""
    if json_ld:
        json_date = dom.json_ld_text(json_ld.get("datePublished") or json_ld.get("dateCreated"))
    if json_date and len(probe["date"]) < 3:
        probe["date"].append({"selector": "json-ld:datePublished", "value": _trunc(json_date, 200)})
    return probe


def _shrink_report(report: dict[str, Any], max_bytes: int = 7900) -> dict[str, Any]:
    """把结构报告压到 max_bytes 以内（Agent 工具返回不应过长）。"""

    def size() -> int:
        return len(json.dumps(report, ensure_ascii=False).encode("utf-8"))

    for _ in range(20):
        if size() <= max_bytes:
            return report
        changed = False
        links = report.get("article_links_sample") or []
        if len(links) > 4:
            report["article_links_sample"] = links[:4]
            changed = True
        candidates = report.get("candidate_selectors") or []
        if not changed and len(candidates) > 3:
            report["candidate_selectors"] = candidates[:3]
            changed = True
        feeds = report.get("feeds") or []
        if not changed and len(feeds) > 3:
            report["feeds"] = feeds[:3]
            changed = True
        probe = report.get("article_page_probe")
        if not changed and isinstance(probe, dict):
            trimmed = False
            for key in ("title", "body", "date"):
                entries = probe.get(key) or []
                if isinstance(entries, list) and len(entries) > 1:
                    probe[key] = entries[:1]
                    trimmed = True
                    break
            if not trimmed:
                for key in ("title", "body", "date"):
                    for entry in probe.get(key) or []:
                        if not isinstance(entry, dict):
                            continue
                        if "sample" in entry:
                            entry["sample"] = _trunc(entry.get("sample"), 80)
                        if "value" in entry:
                            entry["value"] = _trunc(entry.get("value"), 80)
                trimmed = True
            report["article_page_probe"] = probe
            changed = trimmed
        if not changed:
            for link in report.get("article_links_sample") or []:
                if isinstance(link, dict):
                    link["title"] = _trunc(link.get("title"), 40)
            break
    return report


def probe_structure(
    fetcher: Fetcher,
    url: str,
    config: CrawlConfig,
    profile: SiteProfile | None = None,
    settings: dict | None = None,
) -> dict[str, Any]:
    """给 Agent 的 inspect_site 工具：返回页面结构报告（JSON 可序列化，总长 < 8KB）。"""
    discovery = _discovery_settings(settings)
    options = _fetch_options(profile, discovery)
    report: dict[str, Any] = {
        "url": str(url or ""),
        "title": "",
        "language": "",
        "feeds": [],
        "jsonld_types": [],
        "candidate_selectors": [],
        "article_links_sample": [],
        "pagination_hint": "",
        "article_page_probe": {},
        "error": "",
    }
    tmp = DiscoveryResult()
    try:
        fetched = _fetch_page(fetcher, str(url or ""), config, tmp, None, options)
        if fetched is None:
            report["error"] = _trunc(tmp.warnings[0] if tmp.warnings else "页面抓取失败", 200)
            return report
        page_url = fetched.final_url or str(url)
        report["url"] = page_url
        tree = dom.parse_html(fetched.text)
        report["title"] = _trunc(dom.first_value(tree, "title") or dom.first_value(tree, "h1"), 200)
        language = ""
        try:
            languages = tree.xpath("//html/@lang")
            language = str(languages[0]) if languages else ""
        except (AttributeError, TypeError, IndexError):
            language = ""
        if not language:
            language = dom.meta_content(tree, "og:locale", "language", "content-language")
        report["language"] = _trunc(str(language or "").replace("_", "-"), 20)
        report["feeds"] = discover_feeds(
            fetcher, page_url, config, tree=tree, profile=profile, settings=settings
        )[:8]
        objects = dom.json_ld_objects(tree)
        type_names: set[str] = set()
        for obj in objects:
            if isinstance(obj, dict):
                type_names.update(_type_set(obj.get("@type")))
        report["jsonld_types"] = sorted(type_names)[:20]
        report["candidate_selectors"] = candidate_link_selectors(tree, min_count=3)[:6]
        report["pagination_hint"] = _pagination_hint(tree, page_url)

        sample_items: list[NewsItem] = []
        if profile is not None and profile.selector("list", "item"):
            sample_items = _extract_profile_items(tree, page_url, profile, config)
        if len(sample_items) < 3:
            sample_items = sample_items + _jsonld_items(tree, page_url, config)
        if len(sample_items) < 3:
            sample_items = sample_items + _heuristic_items(tree, page_url, config, discovery)
        seen: set[str] = set()
        sample: list[dict[str, str]] = []
        for item in sample_items:
            key = _url_key(item.url)
            if not key or key in seen:
                continue
            seen.add(key)
            sample.append({"url": item.url, "title": _trunc(item.title, 120)})
            if len(sample) >= 10:
                break
        report["article_links_sample"] = sample
        if sample:
            report["article_page_probe"] = _probe_article_page(
                fetcher, sample[0]["url"], config, profile, options
            )
    except Exception as error:  # noqa: BLE001 - inspect 工具不能抛异常
        report["error"] = _trunc(f"{type(error).__name__}: {error}", 300)
    return _shrink_report(report)
