"""文章字段抽取：站点画像 / JSON-LD / OpenGraph / 通用正文算法。

字段抽取优先级（每层失败自动降级）：

- 标题：profile.article.title -> JSON-LD headline/name -> og:title -> h1 -> title 标签
- 正文：profile.article.body -> JSON-LD articleBody -> trafilatura（若安装）-> 内置通用算法
- 时间：profile.article.date -> JSON-LD datePublished/dateCreated -> meta -> time -> URL 日期 -> 标题附近可见日期
- 作者 / 来源 / 摘要 / 栏目 / 图片 / 语言：结构化数据 -> meta -> 通用启发式

extract_from_html 是纯函数（不访问网络），单元测试直接传入 HTML；extract_article
通过 Fetcher 抓取后再调用它，抓取失败抛 FetchError，正文完全抽不到抛 ExtractError。
"""

from __future__ import annotations

import html as _html
import re
from copy import deepcopy
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from lxml import etree
from lxml import html as lxml_html

from . import dom
from .errors import ExtractError, FetchError
from .models import Article, CrawlConfig, NewsItem, SiteProfile
from .normalize import (
    canonicalize_url,
    clean_text,
    detect_language,
    first_sentences,
    normalize_lang_code,
    parse_datetime,
    truncate,
    word_count,
)
from .urls import absolute_url, domain_from_url

__all__ = ["extract_from_html", "extract_article"]

_JSONLD_ARTICLE_TYPES = (
    "newsarticle", "reportagenewsarticle", "analysisnewsarticle", "opinionnewsarticle",
    "reviewnewsarticle", "backgroundnewsarticle", "article", "blogposting",
    "techarticle", "socialmediaposting", "posting",
)

_PARAGRAPH_TAGS = ("p", "blockquote", "pre")
_SKIP_PARAGRAPH_TAGS = ("figcaption",)

_NOISE_LINE_RE = re.compile(
    r"^(?:©|copyright|all rights reserved|all rights|subscribe|sign up|newsletter|"
    r"follow us|share (?:this|on)|advertisement|^ad$|^ads$|read more|related (?:articles|"
    r"stories|coverage|content|topics)|recommended|more from|you may also like|trending|"
    r"most (?:read|popular|viewed)|download (?:the|our) app|get the app|terms of (?:use|service)|"
    r"privacy (?:policy|notice)|cookie (?:policy|settings)|contact us|about us|skip to|"
    r"back to top|^home$|^menu$|^search$|^share$|^comments?$|^topics?$|^tags?$|"
    r"^you have .*left to read|^subscribe|^sign in|^log in|^register)",
    re.IGNORECASE,
)
_NOISE_LINE_CJK_RE = re.compile(
    r"^(?:责任编辑|编辑|来源|原标题|点击(?:查看更多|进入|下载)|扫码|关注(?:我们|公众号)|"
    r"订阅|分享到|相关(?:阅读|推荐|新闻|链接|文章)|推荐阅读|热点推荐|返回首页|"
    r"上一篇|下一页|下一篇|声明|免责声明|版权声明|版权|（完）|\(完\)|全文完|本文来源|"
    r"登录|注册|注销|首页|最新|热门|简|繁|Loading\.?|Android版?|iPhone版?|iPad版?|"
    r"iOS版?|客户端|下载APP|下载客户端|手机版|电脑版|返回顶部|"
    r"报料|举报|热线|邮箱|电话|传真|地址|邮编|广告|商务合作|投稿|"
    r"未经授权|不得转载|版权所有|保留所有权利|"
    r"互联网新闻信息服务许可证|增值电信业务经营许可证|广播电视节目制作经营许可证|"
    r"网络文化经营许可证|信息网络传播视听节目许可证|"
    r"[沪京粤浙苏川渝津]ICP[备证]|[沪京粤浙苏川渝津]公网安备|"
    r"関連記事|関連ニュース|シェア|フォロー|トップへ|ページの先頭|この記事を|"
    r"【PR】|PR[:：]|広告|このページの先頭へ|受信契約|放送法に基づき契約|"
    r"NHK ONE|地図から選ぶ|NHKプラス|受信料)",
    re.IGNORECASE,
)
_NUMERIC_ONLY_RE = re.compile(r"^[-+·•\s]*\d+(?:[.,]\d+)?\s*[kKmMwW]?$")
# 页脚/转载声明等常带媒体名前缀，用子串匹配（仅对短行生效，避免误伤正文）。
_NOISE_SUBSTRING_CJK_RE = re.compile(
    r"(?:报料|举报邮箱|举报电话|未经授权|不得转载|版权所有|保留所有权利|"
    r"互联网新闻信息服务许可证|增值电信业务经营许可证|公网安备|ICP[备证]|"
    r"责任编辑|原标题|点击查看更多|扫码关注|关注公众号|下载客户端|"
    r"免责声明|版权声明|来源[:：]\s*$)",
    re.IGNORECASE,
)
_NOISE_SUBSTRING_EN_RE = re.compile(
    r"(?:of this article left to read|subscribers? only|subscribe to read|"
    r"to continue reading|create a free account|sign up for our newsletter|"
    r"use of cookies|use cookies|cookie settings|cookie policy|consent to use cookies|"
    r"advertising partners|personalized advertising|privacy policy|terms of (?:use|service)|"
    r"all rights reserved|download (?:the|our) app|follow us on|share this article|"
    r"additional revenue from advertising|advertising and subscriptions|"
    r"credit card has expired|update your payment information|subscription running|"
    r"why are you seeing this message)",
    re.IGNORECASE,
)
_BYLINE_PUBLISHED_RE = re.compile(
    r"^(?:By|Par|Von|Por|Di|Door|Автор)\s+.{0,200}?(?:Published|Publié|Veröffentlicht|"
    r"Publicado|Pubblicato|Updated|Mis à jour|Aktualisiert|Actualizado)",
    re.IGNORECASE,
)
_URL_ONLY_RE = re.compile(r"^(?:https?://|www\.)\S+$", re.IGNORECASE)
_DATE_ONLY_RE = re.compile(
    r"^(?:20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?"
    r"|20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?"
    r"|\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}"
    r"|\d{1,2}월\s*\d{1,2}일\s*\d{1,2}:\d{2}"
    r"|\d{1,2}:\d{2}(?::\d{2})?)$"
)
_EMAIL_ONLY_RE = re.compile(r"^[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}$")

_TITLE_SPLIT_RE = re.compile(r"\s+[|｜\-–—·•»/]\s+")
_BAD_TITLE_RE = re.compile(
    r"^(?:home|homepage|首页|首頁|ホーム|accueil|inicio|startseite|главная|"
    r"login|sign in|log in|sign up|register|注册|登录|登入|404|403|not found|"
    r"page not found|error|access denied|forbidden|just a moment|attention required|"
    r"are you a robot|welcome to|请稍候|页面不存在|找不到页面|出错了|bad gateway|"
    r"service unavailable|internal server error|index|untitled)",
    re.IGNORECASE,
)

_POSITIVE_MARKER_RE = re.compile(
    r"article|body|content|story|post|text|entry|main|detail|prose|正文|文章", re.IGNORECASE
)
_NEGATIVE_MARKER_RE = re.compile(
    r"comment|sidebar|footer|header|(?:^|[-_])ad(?:[-_]|s|vert)|nav|menu|share|social|"
    r"related|recommend|breadcrumb|toolbar|newsletter|subscribe|promo|banner|cookie|"
    r"popular|most-read|most-read|tags|byline|widget|scroll|simplebar|modal|drawer|"
    r"popup|overlay|cookie|consent|paywall",
    re.IGNORECASE,
)

_DATE_META_NAMES = (
    "article:published_time", "og:published_time", "publishdate", "pubdate", "publish_date",
    "datepublished", "article.published", "article:post_date", "parsely-pub-date",
    "sailthru.date", "dc.date", "dcterms.date", "date", "article:modified_time",
    "og:updated_time", "last-modified",
)
_AUTHOR_META_NAMES = (
    "author", "article:author", "og:article:author", "twitter:creator",
    "parsely-author", "dc.creator", "byline",
)
_SUMMARY_META_NAMES = ("description", "og:description", "twitter:description")
_IMAGE_META_NAMES = (
    "og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src",
)

_LOWER_CLASS_XPATH = 'translate(@class, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")'
_LOWER_ID_XPATH = 'translate(@id, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")'
_BREADCRUMB_XPATH = (
    f"//*[contains({_LOWER_CLASS_XPATH}, 'breadcrumb') or contains({_LOWER_CLASS_XPATH}, 'crumb') "
    f"or contains({_LOWER_ID_XPATH}, 'breadcrumb')]//a"
)
_BYLINE_XPATH = (
    f"//*[contains({_LOWER_CLASS_XPATH}, 'byline') or contains({_LOWER_CLASS_XPATH}, 'author') "
    f"or contains({_LOWER_CLASS_XPATH}, 'writer') or contains({_LOWER_CLASS_XPATH}, 'reporter') "
    f"or contains(@class, '作者') or contains(@class, '记者') or contains(@id, 'byline')]"
)
_DATE_IN_TEXT_RE = re.compile(
    r"(?:20\d{2}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?"
    r"|\d{1,2}\s*[./]\s*\d{1,2}\s*[./]\s*20\d{2}"
    r"|\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|"
    r"Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s*,?\s*20\d{2}"
    r"|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s*20\d{2})",
    re.IGNORECASE,
)

_ZW_INLINE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
_WHITESPACE_RE = re.compile(r"\s+")
_HOME_SECTION_RE = re.compile(
    r"^(?:home|homepage|首页|首頁|主页|ホーム|accueil|inicio|startseite|главная|"
    r"головна|hem|etisivu|主頁)$",
    re.IGNORECASE,
)
_LANG_SEGMENTS = {
    "en", "de", "fr", "es", "it", "pt", "ja", "zh", "ko", "ru", "ar", "nl", "tr",
    "pl", "sv", "da", "no", "fi", "id", "vi", "cs", "ro", "uk", "el", "he", "hi",
    "th", "www", "index", "index.html", "index.htm", "default", "amp", "article",
    "articles", "story", "stories", "a", "p", "content", "s", "news",
}
_SKIP_SECTION_SEGMENT_RE = re.compile(r"^(?:\d{4}|\d{1,2}|[\d_-]+|.*\.(?:html?|shtml|php|aspx?))$", re.IGNORECASE)
_ARTICLE_SEGMENT_RE = re.compile(
    r"^(?:a-\d+|story\d+|newsdetail_forward_\d+|doc-[a-z0-9]+|video-\d+|"
    r"gallery-\d+|p-\d+|\d{8,})$",
    re.IGNORECASE,
)

_TRAFILATURA_STATE: dict[str, Any] = {"checked": False, "extract": None, "failed": 0}


# ===========================================================================
# 基础工具
# ===========================================================================

def _emit(on_event: Any, message: str, phase: str) -> None:
    """安全触发进度回调（回调异常不影响抽取）。"""
    if on_event is None:
        return
    try:
        on_event(message, phase)
    except Exception:
        pass


def _extract_settings(settings: dict | None) -> dict[str, Any]:
    extraction = (settings or {}).get("extraction") or {}
    return {
        "min_body_chars": int(extraction.get("min_body_chars") or 200),
        "min_confidence": float(extraction.get("min_confidence") or 0.35),
        "max_title_chars": int(extraction.get("max_title_chars") or 300),
    }


def _clean_line(value: Any) -> str:
    """行级清洗：保留段落结构，不做标点裁剪。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    text = _html.unescape(value)
    text = _ZW_INLINE_RE.sub("", text)
    text = text.replace("\u00a0", " ").replace("\u3000", " ")
    return _WHITESPACE_RE.sub(" ", text).strip()


def _dedupe_consecutive(lines: list[str]) -> list[str]:
    result: list[str] = []
    for line in lines:
        if not line:
            continue
        if result and result[-1] == line:
            continue
        result.append(line)
    return result


def _first_value(values: list[str]) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return ""


# ===========================================================================
# JSON-LD / 站点提示
# ===========================================================================

def _article_objects(tree: etree._Element) -> list[dict[str, Any]]:
    objects = dom.json_ld_by_type(tree, _JSONLD_ARTICLE_TYPES)
    if objects:
        return objects
    return [
        obj for obj in dom.json_ld_objects(tree)
        if any(key in obj for key in ("headline", "articleBody", "datePublished"))
    ][:5]


def _jsonld_first(objects: list[dict[str, Any]], keys: tuple[str, ...]) -> str:
    for key in keys:
        for obj in objects:
            text = dom.json_ld_text(obj.get(key))
            if text:
                return text
    return ""


def _person_names(value: Any) -> list[str]:
    names: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, list):
            for entry in item:
                walk(entry)
        elif isinstance(item, dict):
            if item.get("name"):
                names.append(dom.json_ld_text(item.get("name")))
            else:
                parts = [dom.json_ld_text(item.get("givenName")),
                         dom.json_ld_text(item.get("familyName"))]
                joined = " ".join(part for part in parts if part)
                if joined:
                    names.append(joined)
        # 其他类型忽略

    walk(value)
    return [clean_text(name) for name in names if clean_text(name)]


def _site_hints(tree: etree._Element, url: str, profile: SiteProfile | None) -> set[str]:
    hints: set[str] = set()
    site_name = clean_text(dom.meta_content(tree, "og:site_name", "application-name"))
    if site_name:
        hints.add(site_name.lower())
    if profile is not None and profile.name:
        hints.add(clean_text(profile.name).lower())
    domain = domain_from_url(url)
    if domain:
        hints.add(domain.lower())
        stem = domain.split(".")[0]
        if len(stem) >= 3:
            hints.add(stem.lower())
    return {hint for hint in hints if hint}


def _compact_hint(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.lower())


def _is_site_part(part: str, hints: set[str]) -> bool:
    compact = _compact_hint(part)
    if not compact:
        return False
    for hint in hints:
        compact_hint = _compact_hint(hint)
        if len(compact_hint) < 3:
            continue
        if compact == compact_hint or compact_hint in compact or compact in compact_hint:
            return True
    return False


# ===========================================================================
# 标题
# ===========================================================================

def _clean_title(raw: str, hints: set[str]) -> str:
    title = clean_text(raw)
    if not title:
        return ""
    parts = [part.strip() for part in _TITLE_SPLIT_RE.split(title) if part.strip()]
    if len(parts) > 1:
        kept = parts
        if _is_site_part(parts[-1], hints) and len(" ".join(parts[:-1])) >= 10:
            kept = parts[:-1]
        elif _is_site_part(parts[0], hints) and len(" ".join(parts[1:])) >= 10:
            kept = parts[1:]
        if len(kept) < len(parts):
            title = clean_text(" ".join(kept))
    # 去掉常见的「标题 -」结尾残渣
    title = title.strip(" \t\r\n-–—|·•,，;；")
    return title


def _valid_title(title: str, max_chars: int, hints: set[str]) -> bool:
    if not title or len(title) < 2 or len(title) > max_chars:
        return False
    if _BAD_TITLE_RE.match(title.strip()):
        return False
    if _is_site_part(title, hints):
        return False
    return bool(re.search(r"[^\W_]", title))


def _extract_title(
    tree: etree._Element,
    objects: list[dict[str, Any]],
    profile: SiteProfile | None,
    options: dict[str, Any],
    url: str,
) -> tuple[str, str]:
    max_chars = options["max_title_chars"]
    hints = _site_hints(tree, url, profile)
    candidates: list[tuple[str, list[str]]] = []
    if profile is not None:
        selector = profile.selector("article", "title")
        if selector:
            candidates.append(("profile", dom.select_values(tree, selector)))
    headlines = []
    for obj in objects:
        for key in ("headline", "name"):
            text = dom.json_ld_text(obj.get(key))
            if text:
                headlines.append(text)
    if headlines:
        candidates.append(("jsonld", headlines))
    og_title = dom.meta_content(tree, "og:title", "twitter:title")
    if og_title:
        candidates.append(("og", [og_title]))
    candidates.append(("h1", dom.select_values(tree, "h1")))
    candidates.append(("title", dom.select_values(tree, "title")))
    for source, values in candidates:
        for raw in values:
            title = _clean_title(raw, hints)
            if _valid_title(title, max_chars, hints):
                return title, source
    return "", ""


# ===========================================================================
# 发布时间
# ===========================================================================

def _date_from_url(url: str) -> str:
    try:
        path = urlsplit(url).path
    except ValueError:
        return ""
    patterns = (
        r"/(20\d{2})/(\d{1,2})/(\d{1,2})(?:/|$)",
        r"/(20\d{2})-(\d{1,2})-(\d{1,2})(?:/|$)",
        r"/(20\d{2})(\d{2})(\d{2})(?:/|_|\.|-|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, path)
        if not match:
            continue
        year, month, day = (int(group) for group in match.groups())
        try:
            datetime(year, month, day)
        except ValueError:
            continue
        return f"{year:04d}-{month:02d}-{day:02d}"
    return ""


def _title_region_text(tree: etree._Element) -> str:
    pieces: list[str] = []
    nodes = dom.select_nodes(tree, "h1") or dom.select_nodes(tree, "article")
    for node in nodes[:1]:
        current: etree._Element | None = node
        for _ in range(3):
            if current is None:
                break
            pieces.append(dom.node_text(current))
            parent = current.getparent()
            if parent is not None:
                for sibling in list(parent)[:8]:
                    if sibling is current or not isinstance(sibling.tag, str):
                        continue
                    pieces.append(dom.node_text(sibling))
            current = parent
    pieces.append(dom.node_text(tree.xpath("//body")[0]) if tree.xpath("//body") else "")
    return _WHITESPACE_RE.sub(" ", " ".join(pieces))[:4000]


def _visible_date_near_title(tree: etree._Element) -> str:
    text = _title_region_text(tree)
    if not text:
        return ""
    for match in _DATE_IN_TEXT_RE.finditer(text):
        candidate = match.group(0).strip()
        if parse_datetime(candidate):
            return candidate
    return ""


def _extract_published(
    tree: etree._Element,
    objects: list[dict[str, Any]],
    url: str,
    profile: SiteProfile | None,
    config: CrawlConfig,
    html_lang: str,
    item: NewsItem | None,
) -> tuple[str, str, str]:
    candidates: list[tuple[str, str]] = []
    if profile is not None:
        selector = profile.selector("article", "date")
        if selector:
            for value in dom.select_values(tree, selector):
                candidates.append((value, "profile"))
    for key in ("datePublished", "dateCreated", "dateModified"):
        text = _jsonld_first(objects, (key,))
        if text:
            candidates.append((text, "jsonld"))
    for name in _DATE_META_NAMES:
        text = dom.meta_content(tree, name)
        if text:
            candidates.append((text, "meta"))
    for value in dom.select_values(tree, "time::attr(datetime)"):
        candidates.append((value, "time"))
    for value in dom.select_values(tree, "time"):
        candidates.append((value, "time"))
    url_date = _date_from_url(url)
    if url_date:
        candidates.append((url_date, "url"))
    visible = _visible_date_near_title(tree)
    if visible:
        candidates.append((visible, "visible"))

    first_raw = ""
    language = normalize_lang_code(html_lang)
    for raw, source in candidates:
        text = clean_text(raw)
        if not text:
            continue
        first_raw = first_raw or text
        parsed = parse_datetime(
            text,
            default_timezone=config.default_timezone or "UTC",
            language=language,
        )
        if parsed:
            return text, parsed, source
    if item is not None and item.published_raw:
        text = clean_text(item.published_raw)
        parsed = parse_datetime(
            text, default_timezone=config.default_timezone or "UTC", language=language
        )
        if parsed:
            return text, parsed, "item"
        return first_raw or text, "", ""
    return first_raw, "", ""


# ===========================================================================
# 作者 / 来源 / 栏目
# ===========================================================================

_AUTHOR_PREFIX_RE = re.compile(
    r"^(?:written\s+by|words\s+by|by|von|par|por|di|door|автор|作者|記者|记者|撰文|文)\s*[:：]?\s*",
    re.IGNORECASE,
)
_AUTHOR_BAD_RE = re.compile(r"https?://|www\.|@|^[\d\s.,:+-]+$", re.IGNORECASE)


def _clean_author(raw: str) -> str:
    text = clean_text(raw)
    if not text:
        return ""
    text = _AUTHOR_PREFIX_RE.sub("", text).strip()
    text = text.strip(" \t\r\n|·•,，;；-–—:")
    if len(text) < 2 or len(text) > 80:
        return ""
    if _AUTHOR_BAD_RE.search(text):
        return ""
    if parse_datetime(text):
        return ""
    return text


def _extract_author(
    tree: etree._Element,
    objects: list[dict[str, Any]],
    profile: SiteProfile | None,
) -> tuple[str, str]:
    candidates: list[tuple[str, str]] = []
    if profile is not None:
        selector = profile.selector("article", "author")
        if selector:
            for value in dom.select_values(tree, selector):
                candidates.append((value, "profile"))
    for obj in objects:
        names = _person_names(obj.get("author"))
        if names:
            candidates.append((", ".join(names[:3]), "jsonld"))
    for name in _AUTHOR_META_NAMES:
        text = dom.meta_content(tree, name)
        if text:
            candidates.append((text, "meta"))
    for value in dom.select_values(tree, "[rel=author]"):
        candidates.append((value, "rel"))
    try:
        byline_nodes = tree.xpath(_BYLINE_XPATH)[:12]
    except (etree.XPathError, ValueError):
        byline_nodes = []
    for node in byline_nodes:
        candidates.append((dom.node_text(node), "byline"))
    for raw, source in candidates:
        author = _clean_author(raw)
        if author:
            return author, source
    return "", ""


def _extract_source(
    tree: etree._Element,
    objects: list[dict[str, Any]],
    profile: SiteProfile | None,
    url: str,
) -> tuple[str, str]:
    candidates: list[tuple[str, str]] = []
    if profile is not None:
        selector = profile.selector("article", "source")
        if selector:
            for value in dom.select_values(tree, selector):
                candidates.append((value, "profile"))
    site_name = dom.meta_content(tree, "og:site_name")
    if site_name:
        candidates.append((site_name, "og"))
    for obj in objects:
        publisher = obj.get("publisher") or obj.get("sourceOrganization")
        if publisher:
            text = dom.json_ld_text(publisher)
            if text:
                candidates.append((text, "jsonld"))
    for name in ("application-name", "apple-mobile-web-app-title"):
        text = dom.meta_content(tree, name)
        if text:
            candidates.append((text, "meta"))
    domain = domain_from_url(url)
    if domain:
        candidates.append((domain, "domain"))
    for raw, source in candidates:
        text = clean_text(raw).lstrip("@").strip()
        if text and len(text) <= 100:
            return text, source
    return "", ""


def _extract_section(
    tree: etree._Element,
    objects: list[dict[str, Any]],
    profile: SiteProfile | None,
    url: str,
) -> tuple[str, str]:
    candidates: list[tuple[str, str]] = []
    if profile is not None:
        selector = profile.selector("article", "section")
        if selector:
            for value in dom.select_values(tree, selector):
                candidates.append((value, "profile"))
    section = _jsonld_first(objects, ("articleSection",))
    if section:
        candidates.append((section, "jsonld"))
    meta_section = dom.meta_content(tree, "article:section", "og:article:section")
    if meta_section:
        candidates.append((meta_section, "meta"))
    try:
        crumb_nodes = tree.xpath(_BREADCRUMB_XPATH)[:12]
    except (etree.XPathError, ValueError):
        crumb_nodes = []
    crumbs = [clean_text(dom.node_text(node)) for node in crumb_nodes]
    crumbs = [crumb for crumb in crumbs if crumb and not _HOME_SECTION_RE.match(crumb)]
    if crumbs:
        candidates.append((crumbs[-1], "breadcrumb"))
    from_url = _section_from_url(url)
    if from_url:
        candidates.append((from_url, "url"))
    for raw, source in candidates:
        text = _clean_section(raw)
        if text:
            return text, source
    return "", ""


def _clean_section(raw: str) -> str:
    text = clean_text(raw)
    if not text:
        return ""
    text = text.strip(" \t\r\n|·•,，;；>»›-–—:")
    if len(text) > 60 or _HOME_SECTION_RE.match(text):
        return ""
    return text


def _section_from_url(url: str) -> str:
    try:
        path = urlsplit(url).path
    except ValueError:
        return ""
    for segment in (part for part in path.split("/") if part):
        lowered = segment.lower()
        if lowered in _LANG_SEGMENTS and lowered != "news":
            continue
        if _SKIP_SECTION_SEGMENT_RE.match(segment) or _ARTICLE_SEGMENT_RE.match(segment):
            continue
        if len(segment) > 30:
            continue
        if segment.count("-") >= 4 and len(segment) > 20:
            continue  # 文章标题 slug，不是栏目
        cleaned = clean_text(segment.replace("-", " ").replace("_", " "))
        if not cleaned:
            continue
        if cleaned.isascii():
            cleaned = cleaned[:1].upper() + cleaned[1:]
        return cleaned
    return ""


# ===========================================================================
# 正文：站点选择器 / JSON-LD / trafilatura / 通用算法
# ===========================================================================

def _remove_selectors(tree: etree._Element, selectors: Any) -> None:
    if not isinstance(selectors, list):
        return
    for selector in selectors:
        if not isinstance(selector, str) or not selector.strip():
            continue
        for node in dom.select_nodes(tree, selector):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)


def _largest_node(nodes: list[etree._Element]) -> etree._Element | None:
    best: etree._Element | None = None
    best_len = -1
    for node in nodes:
        try:
            length = len(dom.node_text(node))
        except Exception:
            length = 0
        if length > best_len:
            best, best_len = node, length
    return best


def _profile_body(
    tree: etree._Element, profile: SiteProfile | None
) -> tuple[str, etree._Element | None]:
    if profile is None:
        return "", None
    selector = profile.selector("article", "body")
    if not selector:
        return "", None
    nodes = dom.select_nodes(tree, selector)
    if not nodes:
        return "", None
    paragraph_nodes = [
        node for node in nodes
        if isinstance(node.tag, str) and node.tag.lower() in ("p", "blockquote", "pre", "li")
    ]
    if paragraph_nodes:
        texts = [dom.node_text(node) for node in paragraph_nodes]
        return "\n".join(text for text in texts if text), _largest_node(nodes)
    container = _largest_node(nodes)
    if container is None:
        return "", None
    return dom.container_text(container), container


def _html_snippet_to_text(value: Any) -> str:
    raw = str(value or "")
    if not raw.strip():
        return ""
    if "<" not in raw:
        return clean_text(raw)
    try:
        fragment = lxml_html.fragment_fromstring(raw, create_parent="div")
        return dom.container_text(fragment)
    except (etree.ParserError, ValueError):
        return clean_text(re.sub(r"<[^>]+>", " ", raw))


def _jsonld_body(objects: list[dict[str, Any]]) -> str:
    texts: list[str] = []
    for obj in objects:
        for key in ("articleBody", "text"):
            text = _html_snippet_to_text(obj.get(key))
            if text:
                texts.append(text)
    return max(texts, key=len) if texts else ""


def _trafilatura_body(html: str, url: str) -> str:
    if _TRAFILATURA_STATE["failed"] >= 3:
        return ""
    if not _TRAFILATURA_STATE["checked"]:
        _TRAFILATURA_STATE["checked"] = True
        try:
            from trafilatura import extract as trafilatura_extract
            _TRAFILATURA_STATE["extract"] = trafilatura_extract
        except Exception:
            _TRAFILATURA_STATE["extract"] = None
    extract_fn = _TRAFILATURA_STATE.get("extract")
    if extract_fn is None:
        return ""
    try:
        text = extract_fn(html, url=url, include_comments=False, include_tables=False)
    except TypeError:
        try:
            text = extract_fn(html)
        except Exception:
            _TRAFILATURA_STATE["failed"] += 1
            return ""
    except Exception:
        _TRAFILATURA_STATE["failed"] += 1
        return ""
    return (text or "").strip()


def _keep_line(line: str) -> bool:
    if not line or len(line) < 6:
        return False
    if _is_noise_line(line):
        return False
    if not re.search(r"[^\W_]", line):
        return False
    return True


def _line_body(container: etree._Element) -> str:
    try:
        clone = deepcopy(container)
        dom.remove_boilerplate(clone)
        raw = dom.container_text(clone)
    except Exception:
        return ""
    lines = [_clean_line(line) for line in raw.splitlines()]
    lines = [line for line in lines if _keep_line(line)]
    return "\n".join(_dedupe_consecutive(lines))


def _contains_heading(node: etree._Element) -> bool:
    try:
        return bool(node.xpath(".//h1 | .//h2"))
    except (etree.XPathError, ValueError):
        return False


def _score_paragraph_containers(
    tree: etree._Element,
) -> tuple[etree._Element | None, list[str], bool]:
    try:
        paragraphs = tree.xpath(".//p | .//blockquote | .//pre")
    except (etree.XPathError, ValueError):
        paragraphs = []
    scores: dict[int, float] = {}
    containers: dict[int, etree._Element] = {}
    collected: dict[int, list[str]] = {}
    heading_cache: dict[int, bool] = {}
    for paragraph in paragraphs:
        if not isinstance(paragraph.tag, str):
            continue
        parent_tag = paragraph.getparent().tag if paragraph.getparent() is not None else ""
        if parent_tag in _SKIP_PARAGRAPH_TAGS:
            continue
        try:
            ancestors = paragraph.xpath(
                "ancestor::a[1] | ancestor::li[1] | ancestor::nav[1] | "
                "ancestor::aside[1] | ancestor::footer[1]"
            )
        except (etree.XPathError, ValueError):
            ancestors = []
        if ancestors:
            continue  # 链接/列表/导航容器里的短文本不是正文
        text = dom.node_text(paragraph)
        if len(text) < 8:
            continue
        links = paragraph.xpath(".//a")
        link_length = sum(len(dom.node_text(link)) for link in links)
        if link_length and link_length / max(1, len(text)) > 0.5:
            continue
        if _is_noise_line(text):
            continue
        node = paragraph.getparent()
        depth = 0
        while node is not None and depth < 8:
            if not isinstance(node.tag, str):
                break
            marker = f"{node.get('class') or ''} {node.get('id') or ''}".lower()
            if _NEGATIVE_MARKER_RE.search(marker):
                break
            weight = 1.0
            if _POSITIVE_MARKER_RE.search(marker):
                weight *= 1.8
            tag = node.tag.lower()
            if tag in ("article", "main"):
                weight *= 1.6
            elif tag == "body":
                weight *= 0.35
            key = id(node)
            if key not in heading_cache:
                heading_cache[key] = _contains_heading(node)
            if heading_cache[key]:
                weight *= 2.5  # 标题所在区块优先，避免选中页脚/侧栏短文本块
            containers.setdefault(key, node)
            scores[key] = scores.get(key, 0.0) + len(text) * weight
            collected.setdefault(key, []).append(text)
            node = node.getparent()
            depth += 1
    if not scores:
        return None, [], False
    best_key = max(scores, key=lambda key: scores[key])
    return (containers[best_key], _dedupe_consecutive(collected.get(best_key, [])),
            heading_cache.get(best_key, False))


def _generic_body(
    tree: etree._Element, options: dict[str, Any]
) -> tuple[str, etree._Element | None]:
    min_chars = options["min_body_chars"]
    container, paragraphs, has_heading = _score_paragraph_containers(tree)
    text = "\n".join(paragraphs)
    if text and (len(text) >= max(120, min_chars // 2) or has_heading):
        return text, container
    body_nodes = tree.xpath("//body")
    body_node = body_nodes[0] if body_nodes else tree
    for candidate in (container, body_node):
        if candidate is None:
            continue
        fallback = _line_body(candidate)
        if len(fallback) >= max(120, min_chars // 3):
            return fallback, candidate
    fallback = _line_body(body_node)
    return fallback, container or body_node


def _extract_body(
    tree: etree._Element,
    html: str,
    url: str,
    profile: SiteProfile | None,
    config: CrawlConfig,
    options: dict[str, Any],
    objects: list[dict[str, Any]],
) -> tuple[str, etree._Element | None, str]:
    if profile is not None:
        _remove_selectors(tree, profile.article.get("exclude"))
    body, container = _profile_body(tree, profile)
    source = "profile" if len(body) >= 40 else ""
    if len(body) < 40:
        jsonld_text = _jsonld_body(objects)
        if jsonld_text and len(jsonld_text) >= len(body):
            body, container, source = jsonld_text, None, "jsonld"
    if len(body) < 80:
        trafilatura_text = _trafilatura_body(html, url)
        if trafilatura_text and len(trafilatura_text) > len(body):
            body, container, source = trafilatura_text, container, "trafilatura"
    if len(body) < 80:
        generic_text, generic_container = _generic_body(tree, options)
        if len(generic_text) > len(body):
            body = generic_text
            container = generic_container if generic_container is not None else container
            source = "generic"
    return body, container, source or "generic"


def _is_noise_line(line: str) -> bool:
    if not line:
        return False
    if len(line) <= 120 and (_NOISE_LINE_RE.match(line) or _NOISE_LINE_CJK_RE.match(line)):
        return True
    if len(line) <= 80 and _NOISE_SUBSTRING_CJK_RE.search(line):
        return True
    if len(line) <= 2000 and _NOISE_SUBSTRING_EN_RE.search(line):
        return True
    if len(line) <= 260 and _BYLINE_PUBLISHED_RE.match(line):
        return True
    if len(line) <= 16 and re.search(r"(?:微博|公众号|抖音号|视频号|快手|Twitter|Instagram|Facebook)$", line):
        return True
    if _URL_ONLY_RE.match(line) or _DATE_ONLY_RE.match(line):
        return True
    if _EMAIL_ONLY_RE.match(line):
        return True
    if _NUMERIC_ONLY_RE.match(line):
        return True
    return False


def _clean_body_text(raw: str, max_chars: int, title: str = "") -> str:
    if not raw:
        return ""
    text = _html.unescape(str(raw))
    lines: list[str] = []
    for line in re.split(r"\r\n?|\n", text):
        cleaned = _clean_line(line)
        if not cleaned:
            continue
        if title and cleaned == title:
            continue
        probe = re.sub(r"^[-*•·>]\s*", "", cleaned)
        if _is_noise_line(probe):
            continue
        if len(cleaned) < 4 and not re.search(r"[。！？!?；;：:…]$", cleaned):
            continue
        if len(cleaned) <= 40 and cleaned.endswith(">"):
            continue
        lines.append(cleaned)
    lines = _dedupe_consecutive(lines)
    # 新闻正文通常以句号/引号收尾；把尾部没有句末标点的短导航行去掉。
    while len(lines) > 1:
        last = lines[-1]
        if len(last) <= 30 and not re.search(r"[。！？.!?；;…”’」』】）)]]$", last):
            lines.pop()
        else:
            break
    body = "\n".join(lines)
    if not body:
        return ""
    return truncate(body, max_chars) if max_chars and max_chars > 0 else body


# ===========================================================================
# 摘要 / 图片 / 语言 / 置信度
# ===========================================================================

def _extract_summary(
    tree: etree._Element,
    objects: list[dict[str, Any]],
    profile: SiteProfile | None,
    body: str,
    title: str,
) -> tuple[str, str]:
    candidates: list[tuple[str, str]] = []
    if profile is not None:
        selector = profile.selector("article", "summary")
        if selector:
            for value in dom.select_values(tree, selector):
                candidates.append((value, "profile"))
    for obj in objects:
        text = dom.json_ld_text(obj.get("description"))
        if text:
            candidates.append((text, "jsonld"))
    for name in _SUMMARY_META_NAMES:
        text = dom.meta_content(tree, name)
        if text:
            candidates.append((text, "meta"))
    for raw, source in candidates:
        text = clean_text(raw)
        if len(text) < 20 or text == title:
            continue
        return truncate(text, 200), source
    if body:
        text = first_sentences(body, 200)
        if text and text != title:
            return text, "body"
    return "", ""


def _collect_image_urls(value: Any, bucket: list[str]) -> None:
    if isinstance(value, str):
        bucket.append(value)
    elif isinstance(value, list):
        for item in value:
            _collect_image_urls(item, bucket)
    elif isinstance(value, dict):
        for key in ("url", "contentUrl", "thumbnailUrl", "@id"):
            if value.get(key):
                _collect_image_urls(value.get(key), bucket)


def _is_noise_image(url: str, marker: str = "") -> bool:
    low = f"{url} {marker}".lower()
    markers = (
        "logo", "icon", "avatar", "sprite", "gravatar", "blank", "pixel", "spacer",
        "placeholder", "1x1", "transparent", "badge", "emoji", "favicon",
        "thumb", "small", "share", "social", "qrcode", "qr-code", "loading",
        "default", "/_next/static/", "/assets/", "/static/media/", "focusnewssmall",
    )
    if any(marker in low for marker in markers):
        return True
    return low.startswith(("data:", "javascript:", "about:"))


def _extract_images(
    tree: etree._Element,
    objects: list[dict[str, Any]],
    container: etree._Element | None,
    url: str,
) -> list[str]:
    raw_urls: list[tuple[str, str]] = []
    og_image = dom.meta_content(tree, *_IMAGE_META_NAMES)
    if og_image:
        raw_urls.append((og_image, ""))
    for obj in objects:
        for key in ("image", "thumbnailUrl"):
            bucket: list[str] = []
            _collect_image_urls(obj.get(key), bucket)
            raw_urls.extend((item, "") for item in bucket)
    nodes: list[etree._Element] = []
    seen_nodes: set[int] = set()
    if container is not None:
        try:
            for node in container.xpath(".//img"):
                if id(node) not in seen_nodes:
                    seen_nodes.add(id(node))
                    nodes.append(node)
        except (etree.XPathError, ValueError):
            pass
    try:
        for node in tree.xpath("//article//img | //main//img | //figure//img | //picture//img"):
            if id(node) not in seen_nodes:
                seen_nodes.add(id(node))
                nodes.append(node)
    except (etree.XPathError, ValueError):
        pass
    for node in nodes[:80]:
        src = (node.get("src") or node.get("data-src") or node.get("data-lazy-src")
               or node.get("data-original") or "").strip()
        if not src:
            srcset = node.get("srcset") or node.get("data-srcset") or ""
            if srcset:
                src = srcset.split(",")[0].strip().split(" ")[0]
        if not src:
            continue
        marker = f"{node.get('class') or ''} {node.get('alt') or ''}"
        raw_urls.append((src, marker))
    images: list[str] = []
    seen: set[str] = set()
    for raw, marker in raw_urls:
        candidate = raw.strip()
        if not candidate:
            continue
        if _is_noise_image(candidate, marker):
            continue
        absolute = absolute_url(url, candidate)
        if not absolute:
            continue
        key = canonicalize_url(absolute) or absolute
        if key in seen:
            continue
        seen.add(key)
        images.append(absolute)
        if len(images) >= 5:
            break
    return images


def _extract_language(
    body: str, html_lang: str, profile: SiteProfile | None
) -> str:
    language = detect_language(body, html_lang)
    if language:
        return language
    if html_lang:
        normalized = normalize_lang_code(html_lang)
        if normalized:
            return normalized
    if profile is not None:
        normalized = normalize_lang_code(profile.language)
        if normalized:
            return normalized
    return ""


def _confidence(article: Article, options: dict[str, Any]) -> float:
    score = 0.0
    if article.title:
        score += 0.30
    if article.body:
        ratio = min(1.0, len(article.body) / max(1, options["min_body_chars"]))
        score += 0.35 * max(0.4, ratio)
    if article.published_at:
        score += 0.15
    if article.author:
        score += 0.10
    if article.source or article.language:
        score += 0.10
    return round(min(1.0, max(0.0, score)), 2)


# ===========================================================================
# 对外接口
# ===========================================================================

def extract_from_html(
    html: str,
    url: str,
    config: CrawlConfig,
    *,
    profile: SiteProfile | None = None,
    item: NewsItem | None = None,
    settings: dict | None = None,
) -> Article:
    """从文章页 HTML 抽取字段（纯函数，单元测试主入口）。"""
    if isinstance(html, bytes):
        html = html.decode("utf-8", "replace")
    if not isinstance(html, str) or not html.strip():
        raise ExtractError("HTML 内容为空")
    try:
        tree = dom.parse_html(html)
    except (ValueError, etree.ParserError) as error:
        raise ExtractError(f"HTML 解析失败：{error}") from error

    options = _extract_settings(settings)
    objects = _article_objects(tree)
    hints = _site_hints(tree, url, profile)
    html_lang = ""
    try:
        lang_values = tree.xpath("//html/@lang")
        if lang_values:
            html_lang = str(lang_values[0])
    except (etree.XPathError, ValueError):
        pass
    if not html_lang:
        html_lang = dom.meta_content(tree, "og:locale", "content-language")

    title, title_source = _extract_title(tree, objects, profile, options, url)
    if not title and item is not None and item.title:
        fallback = _clean_title(item.title, hints)
        if _valid_title(fallback, options["max_title_chars"], hints):
            title, title_source = fallback, "item"

    published_raw, published_at, date_source = _extract_published(
        tree, objects, url, profile, config, html_lang, item
    )
    author, author_source = _extract_author(tree, objects, profile)
    source, source_source = _extract_source(tree, objects, profile, url)
    section, section_source = _extract_section(tree, objects, profile, url)
    if not section and item is not None and item.section:
        section = clean_text(item.section)
        section_source = "item"

    body_raw, container, body_source = _extract_body(
        tree, html, url, profile, config, options, objects
    )
    body = _clean_body_text(body_raw, config.max_body_chars, title)

    images = _extract_images(tree, objects, container, url)
    if not images and item is not None and item.image:
        fallback_image = absolute_url(url, item.image)
        if fallback_image:
            images = [fallback_image]

    summary, summary_source = _extract_summary(tree, objects, profile, body, title)

    language = _extract_language(body, html_lang, profile)
    if language:
        language_source = "html" if normalize_lang_code(html_lang) else "detect"
    else:
        language_source = ""

    if not title and not body:
        raise ExtractError("页面无法识别为新闻文章（标题与正文均为空）")

    method_parts = [
        f"title={title_source or 'none'}",
        f"body={body_source or 'none'}",
        f"date={date_source or 'none'}",
    ]
    article = Article(
        url=url,
        canonical_url=canonicalize_url(url) or url,
        title=title,
        summary=summary,
        body=body,
        published_at=published_at,
        published_raw=published_raw,
        author=author,
        source=source,
        section=section,
        language=language,
        site_domain=domain_from_url(url),
        images=images,
        extract_method=";".join(method_parts),
        word_count=word_count(body),
        list_page=(item.list_url if item is not None else ""),
    )
    article.confidence = _confidence(article, options)
    return article


def _browser_flag(config: CrawlConfig, profile: SiteProfile | None) -> bool | None:
    mode = ""
    if profile is not None:
        mode = str(profile.fetch.get("mode") or "").lower()
    if mode not in ("auto", "always", "never"):
        mode = ""
    if not mode:
        mode = str(getattr(config, "browser_mode", "auto") or "auto").lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    return None


def extract_article(
    fetcher: Any,
    item: NewsItem,
    config: CrawlConfig,
    *,
    profile: SiteProfile | None = None,
    settings: dict | None = None,
    on_event: Any = None,
) -> Article:
    """抓取并抽取一篇文章；抓取失败抛 FetchError，正文为空抛 ExtractError。"""
    if item is None or not getattr(item, "url", ""):
        raise ExtractError("缺少候选文章 URL")
    _emit(on_event, f"抽取文章：{item.url}", "extract")
    browser = _browser_flag(config, profile)
    wait_selector = ""
    encoding = ""
    if profile is not None:
        wait_selector = str(profile.fetch.get("wait_selector") or "")
        raw_encoding = str(profile.fetch.get("encoding") or "").lower()
        if raw_encoding and raw_encoding != "auto":
            encoding = raw_encoding
    try:
        result = fetcher.get(
            item.url,
            browser=browser,
            wait_selector=wait_selector,
            encoding=encoding,
        )
    except FetchError:
        raise
    except Exception as error:
        raise FetchError(f"抓取失败：{item.url}（{error}）") from error
    if not getattr(result, "ok", False):
        error_text = str(getattr(result, "error", "") or "未知错误")
        raise FetchError(f"抓取失败：{item.url}（{error_text}）")
    html = getattr(result, "text", "") or ""
    final_url = getattr(result, "final_url", "") or item.url
    if not html.strip():
        raise FetchError(f"抓取结果为空：{item.url}")
    article = extract_from_html(
        html, final_url, config, profile=profile, item=item, settings=settings
    )
    if item.list_url:
        article.list_page = item.list_url
    if not article.body:
        raise ExtractError(f"正文抽取失败：{item.url}")
    if on_event is not None:
        _emit(
            on_event,
            f"抽取完成：{article.title[:40]}（{len(article.body)} 字）",
            "extract",
        )
    return article
