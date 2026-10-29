"""归一化工具：多语言日期解析、语言识别、URL 归一、文本清洗、CJK 词数。

本模块只包含纯函数（不访问网络、不读写文件），是单元测试的主要入口：

- canonicalize_url：绝对化 / 去追踪参数 / 小写 host / 去 fragment / 去末尾斜杠；
- parse_datetime：多语言日期 -> ISO 8601（带时区），支持 ISO / RFC 2822 /
  英中德法西意葡荷日韩俄等月名 / 相对时间 / epoch / 仅日期；
- detect_language：优先 html lang，否则按文字脚本与停用词判断；
- clean_text / truncate / first_sentences / word_count。

日期解析只依赖标准库：优先 datetime.fromisoformat / email.utils，时区优先
zoneinfo.ZoneInfo；Windows 缺少 IANA tzdata 时自动回退到固定偏移映射，保证不崩溃。
"""

from __future__ import annotations

import html as _html
import re
import unicodedata
from datetime import datetime, timedelta, timezone, tzinfo
from email.utils import parsedate_to_datetime
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

from .urls import absolute_url, strip_tracking

try:  # pragma: no cover - 标准库在 3.9+ 均可用，保留兜底以防裁剪环境
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

__all__ = [
    "canonicalize_url",
    "parse_datetime",
    "detect_language",
    "normalize_lang_code",
    "clean_text",
    "word_count",
    "truncate",
    "first_sentences",
]


# ===========================================================================
# URL 归一化
# ===========================================================================

_DEFAULT_PORTS = {"http": 80, "https": 443}
_MULTI_SLASH_RE = re.compile(r"/{2,}")


def canonicalize_url(url: str, base_url: str = "") -> str:
    """把 URL 归一化为稳定的 canonical 形式，无法识别时返回空字符串。

    规则：相对链接按 base_url 绝对化；scheme/host 小写；去默认端口；
    路径折叠重复斜杠；去 fragment；去追踪参数并排序剩余参数；
    去末尾斜杠（根路径除外）。
    """
    if not isinstance(url, str):
        return ""
    value = url.strip()
    if not value:
        return ""
    low = value.lower()
    if low.startswith(("http://", "https://")):
        pass
    elif "://" in value:
        return ""  # 非 http(s) 协议（mailto、javascript 等）
    else:
        value = absolute_url(base_url or "", value)
        if not value:
            return ""

    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return ""
    try:
        host = (parts.hostname or "").strip().rstrip(".").lower()
    except ValueError:
        return ""
    if not host:
        return ""
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    netloc_host = f"[{host}]" if ":" in host else host
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc_host}:{port}"
    else:
        netloc = netloc_host

    path = parts.path or "/"
    path = _MULTI_SLASH_RE.sub("/", path)
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    result = urlunsplit((scheme, netloc, path, parts.query, ""))
    result = strip_tracking(result)
    # strip_tracking 会重建 URL（保留 path），这里再兜底去掉末尾斜杠。
    final_parts = urlsplit(result)
    if final_parts.path not in ("", "/") and final_parts.path.endswith("/"):
        result = urlunsplit(
            (final_parts.scheme, final_parts.netloc,
             final_parts.path.rstrip("/"), final_parts.query, "")
        )
    return result


# ===========================================================================
# 文本清洗
# ===========================================================================

_ZW_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
_SPACE_RE = re.compile(r"\s+")
_CJK_PUNCT = "，。！？；：、（）【】《》“”‘’…—"


def clean_text(value: str) -> str:
    """清洗网页字段文本：去 HTML 实体、NFC、NBSP/零宽字符，压缩空白。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    if not value:
        return ""
    text = _html.unescape(value)
    doubled = _html.unescape(text)  # 处理 &amp;#39; 这类双重转义
    text = doubled if doubled != text else text
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\u00a0", " ").replace("\u3000", " ")
    text = _ZW_RE.sub("", text)
    text = _SPACE_RE.sub(" ", text)
    return text.strip(" \t\r\n|·•,，;；")


def truncate(text: str, max_chars: int) -> str:
    """按字符数截断，优先在句末截断并补省略号，结果长度不超过 max_chars。"""
    if not isinstance(text, str) or not text:
        return ""
    limit = max(1, int(max_chars))
    if len(text) <= limit:
        return text
    budget = limit - 1
    if budget <= 0:
        return "…"[:limit]
    head = text[:budget]
    # 在截断窗口后半段找最近的句末标点，避免把一句话拦腰截断。
    cut = -1
    for index in range(len(head) - 1, max(0, int(budget * 0.5)) - 1, -1):
        char = head[index]
        if char in "。！？!?;；.…\n":
            if char == "." and index + 1 < len(head) and head[index + 1].isdigit():
                continue  # 小数/版本号中的点
            cut = index
            break
    if cut >= 0:
        head = head[: cut + 1]
    head = head.rstrip(" \t\r\n")
    if head.endswith(("…", "...")):
        return head[:limit]
    return (head + "…")[:limit]


_CJK_CHAR_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]"
)
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)


def word_count(text: str) -> int:
    """词数统计：CJK（汉字/假名/谚文）每个字符计 1，其他语言按单词计。"""
    if not isinstance(text, str) or not text:
        return 0
    cjk = _CJK_CHAR_RE.findall(text)
    rest = _CJK_CHAR_RE.sub(" ", text)
    words = _WORD_RE.findall(rest)
    return len(cjk) + len(words)


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])|(?<=[.!?])\s+|\n+")


def first_sentences(text: str, max_chars: int = 200) -> str:
    """取正文开头 1-2 句作为摘要，长度不超过 max_chars。"""
    cleaned = clean_text(text)
    if not cleaned:
        return ""
    limit = max(1, int(max_chars))
    if len(cleaned) <= limit:
        return cleaned
    result = ""
    for part in _SENTENCE_SPLIT_RE.split(cleaned):
        part = (part or "").strip()
        if not part:
            continue
        if not result:
            if len(part) > limit:
                return truncate(cleaned, limit)
            result = part
        elif len(result) + len(part) + 1 <= limit:
            result = _join_fragment(result, part)
        else:
            break
        if result and result[-1] in "。！？!?；;":
            break  # 一句话已足够
    return result or truncate(cleaned, limit)


def _join_fragment(left: str, right: str) -> str:
    """拼接句子片段：CJK 之间不加空格，其余加一个空格。"""
    if not left:
        return right
    if not right:
        return left
    if left[-1] in _CJK_PUNCT or _is_cjk_char(left[-1]) or _is_cjk_char(right[0]):
        return left + right
    return left + " " + right


def _is_cjk_char(char: str) -> bool:
    return bool(char) and bool(_CJK_CHAR_RE.match(char))


# ===========================================================================
# 时区
# ===========================================================================

# 固定偏移（分钟）。注意 CST 在不同地区含义不同：中文新闻语境优先按
# 中国标准时间 +08:00 处理；美洲中部时间请显式写 CDT 或 US/Central。
_TZ_ABBREV_MINUTES: dict[str, int] = {
    "UTC": 0, "GMT": 0, "Z": 0,
    "CET": 60, "CEST": 120, "WET": 0, "WEST": 60, "EET": 120, "EEST": 180,
    "EST": -300, "EDT": -240, "CST": 480, "CDT": -300,
    "MST": -420, "MDT": -360, "PST": -480, "PDT": -420,
    "JST": 540, "KST": 540, "IST": 330, "BST": 60,
    "AEST": 600, "AEDT": 660, "ACST": 570, "ACDT": 630,
    "NZST": 720, "NZDT": 780, "MSK": 180, "SGT": 480, "HKT": 480,
    "WIB": 420, "PKT": 300, "ICT": 420, "PHT": 480, "AST": -240, "NST": -210,
}

# Windows 没有 IANA tzdata 时，常见时区名的固定偏移兜底（分钟）。
_IANA_FALLBACK_MINUTES: dict[str, int] = {
    "utc": 0, "etc/utc": 0, "gmt": 0, "etc/gmt": 0,
    "asia/shanghai": 480, "asia/chongqing": 480, "asia/harbin": 480,
    "asia/hong_kong": 480, "asia/macau": 480, "asia/taipei": 480,
    "asia/tokyo": 540, "asia/seoul": 540, "asia/singapore": 480,
    "asia/kolkata": 330, "asia/calcutta": 330, "asia/dubai": 240,
    "asia/bangkok": 420, "asia/jakarta": 420, "asia/manila": 480,
    "europe/london": 0, "europe/dublin": 0, "europe/lisbon": 0,
    "europe/paris": 60, "europe/berlin": 60, "europe/rome": 60,
    "europe/madrid": 60, "europe/amsterdam": 60, "europe/brussels": 60,
    "europe/vienna": 60, "europe/zurich": 60, "europe/stockholm": 60,
    "europe/oslo": 60, "europe/copenhagen": 60, "europe/warsaw": 60,
    "europe/prague": 60, "europe/moscow": 180, "europe/istanbul": 180,
    "america/new_york": -300, "america/toronto": -300, "america/chicago": -360,
    "america/denver": -420, "america/los_angeles": -480,
    "america/vancouver": -480, "america/sao_paulo": -180,
    "america/mexico_city": -360, "australia/sydney": 600,
    "australia/melbourne": 600, "australia/perth": 480,
    "pacific/auckland": 720,
}

_OFFSET_RE = re.compile(r"^(?:(?:UTC|GMT)\s*)?([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def _parse_offset_name(value: str) -> tzinfo | None:
    """解析 +08:00 / UTC+8 / GMT-5 形式的固定偏移。"""
    match = _OFFSET_RE.match((value or "").strip())
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    hours = int(match.group(2))
    minutes = int(match.group(3) or 0)
    if hours > 14 or minutes > 59:
        return None
    total = sign * (hours * 60 + minutes)
    return timezone(timedelta(minutes=total), f"{value.strip().upper()}")


def _timezone_for(name: str) -> tzinfo:
    """把时区名解析为 tzinfo：IANA -> zoneinfo，失败回退固定偏移，最终 UTC。"""
    value = (name or "").strip()
    if not value:
        return timezone.utc
    offset = _parse_offset_name(value)
    if offset is not None:
        return offset
    abbrev = _TZ_ABBREV_MINUTES.get(value.upper())
    if abbrev is not None:
        return timezone(timedelta(minutes=abbrev), value.upper())
    zone = _zoneinfo(value)
    if zone is not None:
        return zone
    fallback = _IANA_FALLBACK_MINUTES.get(value.lower())
    if fallback is not None:
        return timezone(timedelta(minutes=fallback), value)
    return timezone.utc


@lru_cache(maxsize=64)
def _zoneinfo(name: str) -> tzinfo | None:
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:  # ZoneInfoNotFoundError / 非法键 / 缺 tzdata
        return None


_TZ_TOKEN_RE = re.compile(
    r"(?:^|[\s(（\[,，])(?P<token>UTC|GMT|CET|CEST|WET|WEST|EET|EEST|EST|EDT|CST|CDT|"
    r"MST|MDT|PST|PDT|JST|KST|IST|BST|AEST|AEDT|ACST|ACDT|NZST|NZDT|MSK|SGT|HKT|"
    r"WIB|PKT|ICT|PHT|AST|NST)(?:[)）\]]|$|[\s,，])",
    re.IGNORECASE,
)
_UTC_OFFSET_RE = re.compile(r"\b(?:UTC|GMT)\s*([+-]\d{1,2}(?::?\d{2})?)\b", re.IGNORECASE)
_TRAILING_TZ_RE = re.compile(r"[\s,，]*(?P<token>[+-]\d{2}:?\d{2})\s*$")
_TRAILING_WORD_TZ_RE = re.compile(r"[\s,，]+(?P<token>[A-Za-z]{2,5})\s*$")


def _split_timezone(text: str) -> tuple[str, tzinfo | None]:
    """从文本中剥离时区标记，返回 (剩余文本, tzinfo 或 None)。"""
    value = (text or "").strip()
    found: tzinfo | None = None

    match = _UTC_OFFSET_RE.search(value)
    if match:
        found = _parse_offset_name("UTC" + match.group(1))
        value = (value[: match.start()] + " " + value[match.end():]).strip()

    match = _TRAILING_TZ_RE.search(value)
    if match:
        parsed = _parse_offset_name(match.group("token"))
        if parsed is not None:
            found = found or parsed
            value = value[: match.start()].strip()

    match = _TZ_TOKEN_RE.search(value)
    if match:
        token = match.group("token").upper()
        minutes = _TZ_ABBREV_MINUTES.get(token)
        if minutes is not None:
            found = found or timezone(timedelta(minutes=minutes), token)
            value = (value[: match.start()] + " " + value[match.end():]).strip()

    match = _TRAILING_WORD_TZ_RE.search(value)
    if match:
        token = match.group("token").upper()
        minutes = _TZ_ABBREV_MINUTES.get(token)
        if minutes is not None:
            found = found or timezone(timedelta(minutes=minutes), token)
            value = value[: match.start()].strip()

    return value, found


def _attach_tz(value: datetime, default_tz: tzinfo) -> datetime:
    return value.replace(tzinfo=default_tz) if value.tzinfo is None else value


def _to_iso(value: datetime) -> str:
    """输出秒级 ISO 8601（保留原时区偏移）。"""
    return value.replace(microsecond=0, tzinfo=value.tzinfo or timezone.utc).isoformat()


# ===========================================================================
# 日期解析
# ===========================================================================

# 多语言月份名 -> 月份数字。包含常见缩写与俄语属格形式。
_MONTH_NAMES: dict[str, int] = {}


def _register_months(month: int, *names: str) -> None:
    for name in names:
        if name:
            _MONTH_NAMES[name.strip().lower()] = month


_register_months(1, "january", "jan", "januar", "janvier", "enero", "gennaio", "janeiro", "januari")
_register_months(2, "february", "feb", "februar", "février", "fevrier", "febrero", "febbraio", "fevereiro", "februari")
_register_months(3, "march", "mar", "märz", "maerz", "mars", "marzo", "março", "marco", "maart", "marts", "maret")
_register_months(4, "april", "apr", "avril", "abril", "aprile")
_register_months(5, "may", "mai", "mayo", "maggio", "maio", "mei", "maj")
_register_months(6, "june", "jun", "juni", "juin", "junio", "giugno", "junho")
_register_months(7, "july", "jul", "juli", "juillet", "julio", "luglio", "julho")
_register_months(8, "august", "aug", "août", "aout", "agosto", "augustus", "augusti", "agustus")
_register_months(9, "september", "sept", "sep", "septembre", "septiembre", "setiembre", "settembre", "setembro")
_register_months(10, "october", "oct", "oktober", "octobre", "octubre", "ottobre", "outubro")
_register_months(11, "november", "nov", "novembre", "noviembre", "novembro")
_register_months(12, "december", "dec", "dezember", "décembre", "decembre", "diciembre", "dicembre", "dezembro", "desember")
# 俄语（主格 + 属格）
_register_months(1, "январь", "января")
_register_months(2, "февраль", "февраля")
_register_months(3, "март", "марта")
_register_months(4, "апрель", "апреля")
_register_months(5, "май", "мая")
_register_months(6, "июнь", "июня")
_register_months(7, "июль", "июля")
_register_months(8, "август", "августа")
_register_months(9, "сентябрь", "сентября")
_register_months(10, "октябрь", "октября")
_register_months(11, "ноябрь", "ноября")
_register_months(12, "декабрь", "декабря")
# 土耳其/波兰/捷克/罗马尼亚
_register_months(1, "ocak", "styczeń", "stycznia", "leden", "ledna", "ianuarie")
_register_months(2, "şubat", "subat", "luty", "lutego", "únor", "unor", "februarie")
_register_months(3, "mart", "marzec", "marca", "březen", "brezen", "martie")
_register_months(4, "nisan", "kwiecień", "kwietnia", "kwiecien", "duben", "dubna", "aprilie")
_register_months(5, "mayıs", "mayis", "maja", "květen", "kveten")
_register_months(6, "haziran", "czerwiec", "czerwca", "červen", "cerven", "iunie")
_register_months(7, "temmuz", "lipiec", "lipca", "červenec", "cervenec", "iulie")
_register_months(8, "ağustos", "agustos", "sierpień", "sierpnia", "sierpien", "srpen", "srpna")
_register_months(9, "eylül", "eylul", "wrzesień", "września", "wrzesnia", "září", "zari", "septembrie")
_register_months(10, "ekim", "październik", "października", "pazdziernika", "říjen", "rijen", "octombrie")
_register_months(11, "kasım", "kasim", "listopada", "noiembrie")
_register_months(12, "aralık", "aralik", "grudzień", "grudnia", "grudzien", "prosinec", "prosince", "decembrie")
# 芬兰语
_register_months(1, "tammikuu", "tammikuuta")
_register_months(2, "helmikuu", "helmikuuta")
_register_months(3, "maaliskuu", "maaliskuuta")
_register_months(4, "huhtikuu", "huhtikuuta")
_register_months(5, "toukokuu", "toukokuuta")
_register_months(6, "kesäkuu", "kesakuu", "kesäkuuta")
_register_months(7, "heinäkuu", "heinakuu", "heinäkuuta")
_register_months(8, "elokuu", "elokuuta")
_register_months(9, "syyskuu", "syyskuuta")
_register_months(10, "lokakuu", "lokakuuta")
_register_months(11, "marraskuu", "marraskuuta")
_register_months(12, "joulukuu", "joulukuuta")

_MONTH_PATTERN = re.compile(
    r"(?<![\w])(?:"
    + "|".join(sorted((re.escape(name) for name in _MONTH_NAMES), key=len, reverse=True))
    + r")\.?(?![\w])",
    re.IGNORECASE,
)

_LABEL_RE = re.compile(
    r"^\s*(?:published|posted|updated|last updated|date|"
    r"publié(?: le)?|mis à jour(?: le)?|veröffentlicht(?: am)?|aktualisiert(?: am)?|"
    r"publicado(?: el)?|actualizado(?: el)?|pubblicato(?: il)?|aggiornato|"
    r"gepubliceerd(?: op)?|bijgewerkt(?: op)?|yayınlanma tarihi|güncellenme|"
    r"发布日期?|发布时间?|發表日期?|發佈時間?|更新时间?|更新時間?|"
    r"公開日|更新日|掲載日|投稿日|配信日)\s*(?:on|am|le|el|il|op|tarihi|:)?\s*[:：]?\s*",
    re.IGNORECASE,
)
_ORDINAL_RE = re.compile(r"(?<=\d)(?:st|nd|rd|th)\b", re.IGNORECASE)
_WEEKDAY_PAREN_RE = re.compile(
    r"[（(](?:月|火|水|木|金|土|日|Mon|Tue|Wed|Thu|Fri|Sat|Sun)[)）]",
    re.IGNORECASE,
)

_CJK_DATE_RE = re.compile(
    r"(?P<y>\d{4})\s*[年년]\s*(?P<m>\d{1,2})\s*[月월]\s*(?P<d>\d{1,2})\s*[日일]?"
)
_CJK_MONTH_DAY_RE = re.compile(r"(?P<m>\d{1,2})\s*[月월]\s*(?P<d>\d{1,2})\s*[日일]")
_CJK_TIME_RE = re.compile(
    r"(?P<ampm>上午|下午|晚上|中午|凌晨|早上|午前|午後|오전|오후)?\s*"
    r"(?P<h>\d{1,2})\s*[时時시:]\s*(?P<mi>\d{1,2})?"
    r"(?:\s*[分분:]\s*(?P<s>\d{1,2}))?"
)
_TIME_COLON_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?:[.,](\d{1,6}))?")
_TIME_HOUR_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[hH]\s*(\d{2})(?!\d)")
_TIME_CN_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*(?:Uhr|時|时|시)\s*(?:(\d{1,2})\s*(?:分|분)?)?",
    re.IGNORECASE,
)
_AMPM_RE = re.compile(r"(?<!\w)(a\.m\.|p\.m\.|am|pm)(?!\w)", re.IGNORECASE)
_AMPM_CJK_RE = re.compile(r"(上午|下午|晚上|中午|凌晨|早上|午前|午後|오전|오후)")

_NAMED_RELATIVE: dict[str, int] = {
    # 值：相对“今天”的天数；0 今天，-1 昨天，-2 前天。
    "just now": 0, "right now": 0, "now": 0, "a moment ago": 0, "moments ago": 0,
    "today": 0, "yesterday": -1, "the day before yesterday": -2,
    "刚刚": 0, "刚才": 0, "方才": 0, "现在": 0, "今天": 0, "今日": 0,
    "昨天": -1, "昨日": -1, "前天": -2,
    "たった今": 0, "ついさっき": 0, "今": 0, "今日": 0, "きょう": 0,
    "昨日": -1, "きのう": -1, "一昨日": -2,
    "방금": 0, "지금": 0, "오늘": 0, "어제": -1, "그저께": -2,
    "gerade eben": 0, "soeben": 0, "heute": 0, "gestern": -1, "vorgestern": -2,
    "à l'instant": 0, "a l'instant": 0, "aujourd'hui": 0, "hier": -1, "avant-hier": -2,
    "ahora mismo": 0, "ahora": 0, "hoy": 0, "ayer": -1, "anteayer": -2,
    "adesso": 0, "oggi": 0, "ieri": -1, "l'altro ieri": -2,
    "agora": 0, "hoje": 0, "ontem": -1, "anteontem": -2,
    "nu": 0, "vandaag": 0, "gisteren": -1, "eergisteren": -2,
    "şimdi": 0, "bugün": 0, "dün": -1, "evvelsi gün": -2,
    "teraz": 0, "dzisiaj": 0, "dziś": 0, "wczoraj": -1, "przedwczoraj": -2,
    "сейчас": 0, "только что": 0, "сегодня": 0, "вчера": -1, "позавчера": -2,
}

_UNIT_SECONDS: dict[str, float] = {}


def _register_units(seconds: float, *names: str) -> None:
    for name in names:
        if name:
            _UNIT_SECONDS[name.strip().lower()] = seconds


_register_units(1, "second", "seconds", "sec", "secs", "s")
_register_units(60, "minute", "minutes", "min", "mins", "m")
_register_units(3600, "hour", "hours", "hr", "hrs", "h")
_register_units(86400, "day", "days", "d")
_register_units(7 * 86400, "week", "weeks", "wk", "wks", "w")
_register_units(30 * 86400, "month", "months")
_register_units(365 * 86400, "year", "years", "yr", "yrs")
_register_units(1, "sekunde", "sekunden", "sek", "secondes", "seconde", "segundo", "segundos",
                "secondo", "secondi", "seconde", "seconden", "saniye", "sekund", "sekundy",
                "секунда", "секунды", "секунд", "секунду")
_register_units(60, "minuten", "minute", "minuto", "minutos", "minuti", "minuut",
                "dakika", "minut", "minuty", "минута", "минуты", "минут", "минуту")
_register_units(3600, "stunde", "stunden", "heure", "heures", "hora", "horas",
                "ora", "ore", "uur", "uren", "saat", "godzina", "godziny", "godzin",
                "час", "часа", "часов")
_register_units(86400, "tag", "tage", "tagen", "jour", "jours", "día", "días", "dia",
                "dias", "giorno", "giorni", "dag", "dagen", "gün", "dzień", "dni",
                "день", "дня", "дней")
_register_units(7 * 86400, "woche", "wochen", "semaine", "semaines", "semana", "semanas",
                "settimana", "settimane", "week", "weken", "hafta", "tydzień", "tygodni",
                "неделя", "недели", "недель")
_register_units(30 * 86400, "monat", "monate", "monaten", "mois", "mes", "meses", "mese",
                "mesi", "mês", "maand", "maanden", "ay", "miesiąc", "miesiące", "miesięcy",
                "месяц", "месяца", "месяцев")
_register_units(365 * 86400, "jahr", "jahre", "jahren", "an", "ans", "années", "année",
                "año", "años", "ano", "anos", "anno", "anni", "jaar", "jaren", "yıl",
                "rok", "lata", "lat", "год", "года", "лет")
_register_units(1, "秒", "초")
_register_units(60, "分", "分钟", "分鐘", "분")
_register_units(3600, "小时", "小時", "時間", "时间", "시간")
_register_units(86400, "天", "日", "일")
_register_units(7 * 86400, "周", "週", "星期", "週間", "주")
_register_units(30 * 86400, "个月", "個月", "ヶ月", "か月", "개월")
_register_units(365 * 86400, "年", "년")

_RELATIVE_PATTERNS = (
    re.compile(
        r"(?P<n>\d+|an?|one|a few)\s*"
        r"(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|wks?|months?|years?|yrs?)"
        r"\s+ago\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"vor\s+(?P<n>\d+|einer?|ein(?:er)?|zwei|drei|vier|fünf|sechs|sieben|acht|neun|zehn)\s*"
        r"(?P<unit>sekunden?|minuten?|stunden?|tagen?|wochen?|monaten?|jahren?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"il y a\s+(?P<n>\d+|une?|deux|trois|quatre|cinq|six|sept|huit|neuf|dix)\s*"
        r"(?P<unit>secondes?|minutes?|heures?|jours?|semaines?|mois|ans?|années?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"hace\s+(?P<n>\d+|una?|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\s*"
        r"(?P<unit>segundos?|minutos?|horas?|días?|dias?|semanas?|mes(?:es)?|años?|anos?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<n>\d+|un[ao]?)\s*"
        r"(?P<unit>second[oi]|minut[oi]|or[ae]|giorn[oi]|settiman[ae]|mes[ei]|ann[oi])\s+fa\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"h[áa]\s+(?P<n>\d+|uma?)\s*"
        r"(?P<unit>segundos?|minutos?|horas?|dias?|semanas?|m[eê]s|meses|anos?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<n>\d+|een|twee|drie|vier|vijf|zes|zeven|acht|negen|tien)\s*"
        r"(?P<unit>seconde[n]?|minuut|minuten|uur|uren|dag(?:en)?|week|weken|maand(?:en)?|jaar|jaren)"
        r"\s+geleden",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<n>\d+|bir|iki|üç|dört|beş|altı|yedi|sekiz|dokuz|on)\s*"
        r"(?P<unit>saniye|dakika|saat|gün|hafta|ay|yıl)\s+önce",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<n>\d+|jeden|dwa|trzy|cztery|pięć|sześć|siedem|osiem|dziewięć|dziesięć)\s*"
        r"(?P<unit>sekund\w*|minut\w*|godzin\w*|dni|tydzień|tygodni|miesiąc\w*|rok|lat)\s+temu",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<n>\d+|один|одну|два|три|четыре|пять|шесть|семь|восемь|девять|десять)\s*"
        r"(?P<unit>секунд\w*|минут\w*|час\w*|день|дня|дней|недел\w*|месяц\w*|год|года|лет)"
        r"\s+назад",
        re.IGNORECASE,
    ),
    re.compile(r"(?P<n>\d+)\s*(?P<unit>秒|分钟|分鐘|分|小时|小時|天|日|周|週|星期|个月|個月|年)\s*前"),
    re.compile(r"(?P<n>\d+)\s*(?P<unit>秒|分|時間|日|週間|週|か月|ヶ月|年)\s*前"),
    re.compile(r"(?P<n>\d+)\s*(?P<unit>초|분|시간|일|주|개월|년)\s*전"),
)

_NUMBER_WORDS = {
    "an": 1, "a": 1, "one": 1, "une": 1, "un": 1, "una": 1, "uno": 1, "uma": 1, "um": 1,
    "een": 1, "einer": 1, "eine": 1, "ein": 1, "bir": 1, "jeden": 1, "один": 1, "одну": 1,
    "zwei": 2, "deux": 2, "dos": 2, "due": 2, "twee": 2, "iki": 2, "dwa": 2, "два": 2,
    "drei": 3, "trois": 3, "tres": 3, "tre": 3, "drie": 3, "üç": 3, "trzy": 3, "три": 3,
    "vier": 4, "quatre": 4, "cuatro": 4, "quattro": 4, "dört": 4, "cztery": 4, "четыре": 4,
    "fünf": 5, "cinq": 5, "cinco": 5, "cinque": 5, "vijf": 5, "beş": 5, "pięć": 5, "пять": 5,
    "sechs": 6, "six": 6, "seis": 6, "sei": 6, "zes": 6, "altı": 6, "sześć": 6, "шесть": 6,
    "sieben": 7, "sept": 7, "siete": 7, "sette": 7, "zeven": 7, "yedi": 7, "siedem": 7, "семь": 7,
    "acht": 8, "huit": 8, "ocho": 8, "otto": 8, "sekiz": 8, "osiem": 8, "восемь": 8,
    "neun": 9, "neuf": 9, "nueve": 9, "nove": 9, "negen": 9, "dokuz": 9, "dziewięć": 9, "девять": 9,
    "zehn": 10, "dix": 10, "diez": 10, "dieci": 10, "tien": 10, "on": 10, "dziesięć": 10, "десять": 10,
}

_EPOCH_RE = re.compile(r"^-?\d{9,13}(?:\.\d+)?$")
_COMPACT_DATETIME_RE = re.compile(
    r"^(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})(?:[T ]?(?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2})?"
    r"\s*(?P<tz>Z|[+-]\d{2}:?\d{2})?)?$"
)


def _prepare_datetime_text(raw: object) -> str:
    """统一日期原始文本：去 HTML 实体、标签、标签词、序数后缀、异常空白。"""
    if raw is None:
        return ""
    if isinstance(raw, datetime):
        return ""
    if not isinstance(raw, str):
        raw = str(raw)
    text = _html.unescape(raw)
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    text = re.sub(r"<[^>]+>", " ", text)
    text = _ZW_RE.sub("", text)
    text = re.sub(r"[\u00a0\u2000-\u200b\u202f\u205f\u3000\ufeff]", " ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    text = _LABEL_RE.sub("", text, count=1)
    text = _LABEL_RE.sub("", text, count=1)
    text = _ORDINAL_RE.sub("", text)
    text = _WEEKDAY_PAREN_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text.strip(" \t\r\n|·•,，;；:：-")


def _parse_epoch(text: str) -> datetime | None:
    if not _EPOCH_RE.match(text):
        return None
    try:
        timestamp = float(text)
    except (TypeError, ValueError):
        return None
    if abs(timestamp) > 1e11:  # 毫秒时间戳
        timestamp /= 1000.0
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _coerce_number(token: str) -> int | None:
    token = (token or "").strip().lower()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def _parse_relative(text: str, now: datetime, default_tz: tzinfo) -> datetime | None:
    """解析相对时间：2 hours ago / vor 2 Stunden / 3天前 / 昨天 ..."""
    lowered = text.strip().lower()
    for name, days in sorted(_NAMED_RELATIVE.items(), key=lambda item: len(item[0]), reverse=True):
        if lowered == name:
            base = now if now.tzinfo else now.replace(tzinfo=default_tz)
            target = base + timedelta(days=days)
            if days < 0 or name in ("today", "今天", "今日", "きょう"):
                return target.replace(hour=0, minute=0, second=0, microsecond=0)
            return target
    for pattern in _RELATIVE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        number = _coerce_number(match.group("n"))
        unit = match.group("unit").strip().lower()
        seconds = _UNIT_SECONDS.get(unit)
        if number is None or seconds is None:
            continue
        base = now if now.tzinfo else now.replace(tzinfo=default_tz)
        return base - timedelta(seconds=number * seconds)
    return None


def _parse_iso(text: str, default_tz: tzinfo) -> datetime | None:
    try:
        value = datetime.fromisoformat(text)
        return _attach_tz(value, default_tz)
    except ValueError:
        pass
    match = _COMPACT_DATETIME_RE.match(text)
    if match:
        try:
            return datetime(
                int(match.group("y")), int(match.group("m")), int(match.group("d")),
                int(match.group("h") or 0), int(match.group("mi") or 0),
                int(match.group("s") or 0), tzinfo=default_tz,
            )
        except ValueError:
            return None
    return None


def _parse_rfc2822(text: str, default_tz: tzinfo) -> datetime | None:
    try:
        value = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if value is None:
        return None
    return _attach_tz(value, default_tz)


def _find_time(text: str) -> tuple[int, int, int, int, int] | None:
    """在文本中找第一个时间，返回 (时, 分, 秒, start, end)。"""
    match = _TIME_COLON_RE.search(text)
    if not match:
        match = _TIME_HOUR_RE.search(text)
    if not match:
        match = _TIME_CN_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    second = int(match.group(3) or 0) if match.re is _TIME_COLON_RE else 0
    start, end = match.span()
    window = text[max(0, start - 8): min(len(text), end + 10)]
    ampm_match = _AMPM_RE.search(window)
    if ampm_match:
        token = ampm_match.group(0).lower().replace(".", "")
        if token == "pm" and hour < 12:
            hour += 12
        elif token == "am" and hour == 12:
            hour = 0
    else:
        amp = _AMPM_CJK_RE.search(window)
        if amp:
            token = amp.group(1)
            if token in ("下午", "晚上", "午後", "오후") and hour < 12:
                hour += 12
            elif token in ("上午", "中午", "凌晨", "早上", "午前", "오전") and hour == 12:
                hour = 0
    if hour > 23 or minute > 59 or second > 59:
        return None
    return hour, minute, second, start, end


def _blank_span(text: str, start: int, end: int) -> str:
    """把 [start, end) 替换为等长空格（保持字符位置不变）。"""
    return text[:start] + " " * max(0, end - start) + text[end:]


def _valid_date(year: int, month: int, day: int) -> bool:
    return 1900 <= year <= 2200 and 1 <= month <= 12 and 1 <= day <= 31


def _parse_cjk_date(text: str, now: datetime, default_tz: tzinfo) -> datetime | None:
    match = _CJK_DATE_RE.search(text)
    if match:
        year, month, day = int(match.group("y")), int(match.group("m")), int(match.group("d"))
    else:
        match = _CJK_MONTH_DAY_RE.search(text)
        if not match:
            return None
        year = now.year
        month, day = int(match.group("m")), int(match.group("d"))
    if not _valid_date(year, month, day):
        return None
    hour = minute = second = 0
    time_match = _CJK_TIME_RE.search(text, match.end())
    if time_match:
        hour = int(time_match.group("h") or 0)
        minute = int(time_match.group("mi") or 0)
        second = int(time_match.group("s") or 0)
        ampm = time_match.group("ampm") or ""
        if ampm in ("下午", "晚上", "午後", "오후") and hour < 12:
            hour += 12
        elif ampm in ("上午", "中午", "凌晨", "早上", "午前", "오전") and hour == 12:
            hour = 0
    try:
        value = datetime(year, month, day, hour, minute, second, tzinfo=default_tz)
    except ValueError:
        return None
    if match.re is _CJK_MONTH_DAY_RE and value > now + timedelta(days=30):
        try:
            value = value.replace(year=year - 1)
        except ValueError:
            pass
    return value


def _parse_named_month(text: str, now: datetime, default_tz: tzinfo) -> datetime | None:
    month_match = _MONTH_PATTERN.search(text)
    if not month_match:
        return None
    month_key = month_match.group(0).rstrip(".").lower()
    month = _MONTH_NAMES.get(month_key)
    if month is None:
        return None
    time_info = _find_time(text)
    work = text
    hour = minute = second = 0
    if time_info:
        hour, minute, second, start, end = time_info
        work = _blank_span(text, start, end)
    numbers: list[tuple[int, int]] = []
    for number_match in re.finditer(r"(?<!\d)(\d{1,4})(?!\d)", work):
        position = number_match.start()
        if not (number_match.end() <= month_match.start() or position >= month_match.end()):
            continue
        numbers.append((position, int(number_match.group(1))))
    year = next((value for _, value in numbers if 1900 <= value <= 2200), 0)
    before = [value for position, value in numbers if position < month_match.start() and value != year]
    after = [value for position, value in numbers if position > month_match.start() and value != year]
    day_candidates = [value for value in (before[::-1] + after) if 1 <= value <= 31]
    if not day_candidates:
        return None
    day = day_candidates[0]
    if not year:
        year = now.year
    if not _valid_date(year, month, day):
        return None
    try:
        value = datetime(year, month, day, hour, minute, second, tzinfo=default_tz)
    except ValueError:
        return None
    if not any(1900 <= item <= 2200 for _, item in numbers) and value > now + timedelta(days=30):
        try:
            value = value.replace(year=year - 1)
        except ValueError:
            pass
    return value


def _parse_numeric_date(
    text: str, language: str, now: datetime, default_tz: tzinfo
) -> datetime | None:
    time_info = _find_time(text)
    work = text
    hour = minute = second = 0
    if time_info:
        hour, minute, second, start, end = time_info
        work = _blank_span(text, start, end)
    year = month = day = 0
    match = re.search(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)", work)
    if match:
        year, month, day = (int(group) for group in match.groups())
    else:
        match = re.search(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)", work)
        if match:
            year, month, day = (int(group) for group in match.groups())
        else:
            match = re.search(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})(?!\d)", work)
            if not match:
                return None
            first, second_value, third = (int(group) for group in match.groups())
            lang = normalize_lang_code(language)
            if lang in ("en", "fil") and first <= 12 and second_value <= 12:
                month, day = first, second_value
            else:
                day, month = first, second_value
            if not 1 <= month <= 12 and 1 <= day <= 12:
                month, day = day, month
            year = third if third > 99 else (2000 + third if third < 70 else 1900 + third)
    if not _valid_date(year, month, day):
        return None
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=default_tz)
    except ValueError:
        return None


def parse_datetime(
    raw: str,
    *,
    default_timezone: str = "UTC",
    now: datetime | None = None,
    language: str = "",
) -> str:
    """把多语言日期/时间文本归一化为 ISO 8601（带时区），失败返回空字符串。

    now 仅用于相对时间（2 hours ago / 3天前）与缺年份的日期，便于测试注入。
    """
    if isinstance(raw, datetime):
        return _to_iso(_attach_tz(raw, _timezone_for(default_timezone)))
    text = _prepare_datetime_text(raw)
    if not text:
        return ""
    default_tz = _timezone_for(default_timezone)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    # 1) epoch 秒/毫秒
    value = _parse_epoch(text)
    if value:
        return _to_iso(value)

    # 2) 相对时间（ago / vor / il y a / 前 / 昨天 ...）
    value = _parse_relative(text, current, default_tz)
    if value:
        return _to_iso(value)

    # 3) 剥离时区标记后按 ISO / RFC 2822 / 各语言格式解析
    body_text, tz_hint = _split_timezone(text)
    tz = tz_hint or default_tz

    value = _parse_iso(body_text, tz)
    if value:
        return _to_iso(value)

    value = _parse_cjk_date(body_text, current, tz)
    if value:
        return _to_iso(value)

    value = _parse_named_month(body_text, current, tz)
    if value:
        return _to_iso(value)

    # RFC 2822 放在具名月份之后：email.utils 会忽略 "3:45 PM" 的 PM 标记。
    value = _parse_rfc2822(body_text, tz)
    if value:
        return _to_iso(value)

    value = _parse_numeric_date(body_text, language, current, tz)
    if value:
        return _to_iso(value)
    return ""


# ===========================================================================
# 语言识别
# ===========================================================================

_LANG_ALIASES = {
    "jp": "ja", "jpn": "ja", "in": "id", "ind": "id", "iw": "he", "heb": "he",
    "ji": "yi", "jw": "jv", "mo": "ro", "ron": "ro", "rum": "ro", "sh": "sr",
    "ze": "zh", "zho": "zh", "chi": "zh", "chn": "zh", "cht": "zh",
    "ger": "de", "deu": "de", "fre": "fr", "fra": "fr", "dut": "nl", "nld": "nl",
    "cze": "cs", "ces": "cs", "swe": "sv", "dan": "da", "nor": "no", "nob": "no",
    "nno": "no", "fin": "fi", "pol": "pl", "rus": "ru", "kor": "ko", "spa": "es",
    "ita": "it", "por": "pt", "tur": "tr", "ara": "ar", "hin": "hi", "tha": "th",
    "ukr": "uk", "ell": "el", "gre": "el", "vie": "vi", "msa": "ms", "may": "ms",
    "eng": "en", "sqi": "sq", "alb": "sq", "fas": "fa", "per": "fa",
}
_ZH_CODES = {"zh-hans", "zh-hant", "zh-cn", "zh-tw", "zh-sg", "zh-hk", "zh-mo", "zh-chs", "zh-cht"}


def normalize_lang_code(value: str) -> str:
    """规范化 HTML lang / og:locale 语言代码：en-US 记为 en、zh-Hans 记为 zh。"""
    if not isinstance(value, str):
        return ""
    code = value.strip().lower().replace("_", "-")
    if not code:
        return ""
    if code in _ZH_CODES:
        return "zh"
    base = code.split("-")[0]
    base = _LANG_ALIASES.get(base, base)
    if base in ("und", "mul", "zxx", "qaa", ""):
        return ""
    return base if re.fullmatch(r"[a-z]{2,3}", base) else ""


_LATIN_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset("the and of to in is that for on with as was at by from it this are be has had not but they his her their we you which will can have been or an were said more than about also its".split()),
    "fr": frozenset("le la les des du de et en un une est que qui dans pour pas au aux ce cette sur avec par plus sont mais ou où être au si leur ses son sa nous vous ils elles ont fait".split()),
    "de": frozenset("der die das und in den von zu mit sich des auf für ist im dem nicht ein eine als auch es an werden aus er hat dass sie nach wird bei um am wie aber vom zum zur durch war haben".split()),
    "es": frozenset("el la los las de del y en un una que por para con no se su al es son como más pero sus le ya este entre cuando muy será sobre también".split()),
    "it": frozenset("il lo la i gli le di del e in un una che per con non si su al è sono come più ma della dei questo tra anche nel alla stato ha".split()),
    "pt": frozenset("o a os as de do da dos das e em um uma que por para com não se su ao é são como mais mas foi ser esta neste pelo pela também".split()),
    "nl": frozenset("de het een van en in is dat op te zijn met voor niet aan er ook als bij maar om dan nog worden door heeft zich werd deze meer".split()),
    "tr": frozenset("ve bir bu da de için ile olarak çok daha ne var olan gibi ama en o şey kadar sonra her tarafından olduğu yeni büyük ilk son göre karşı yıl gün kez dedi açıkladı".split()),
    "pl": frozenset("i w z na do się nie jest że to oraz jak ale po od dla przez co tak już czy być są jego tej tym było".split()),
    "sv": frozenset("och i att det som en på är av för med till den har inte om ett var de så kan från vid också efter".split()),
    "da": frozenset("og i at det som en på er af for med til den har ikke om et var de så kan fra ved også efter".split()),
    "no": frozenset("og i at det som en på er av for med til den har ikke om et var de så kan fra ved også etter".split()),
    "fi": frozenset("ja on ei että se hän oli joka mutta kun niin myös tai jos sen ovat voi vain mitä tämä sekä".split()),
    "id": frozenset("dan yang di ini itu dengan untuk dari tidak akan pada juga dalam adalah sebagai oleh ke kata mereka".split()),
    "vi": frozenset("và của các có trong là được cho với không một những này đã để khi người từ theo về".split()),
    "cs": frozenset("a v se na je že do to s z pro by ale jako po od kdy tak jsou který bylo".split()),
    "ro": frozenset("și în de la cu pe un o este care din pentru nu se mai ca fi fost sunt lui".split()),
}

_SCRIPT_PATTERNS: dict[str, re.Pattern] = {
    "ja": re.compile(r"[\u3040-\u30ff]"),
    "ko": re.compile(r"[\uac00-\ud7af\u1100-\u11ff]"),
    "zh": re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"),
    "ru": re.compile(r"[\u0400-\u04ff]"),
    "ar": re.compile(r"[\u0600-\u06ff\u0750-\u077f]"),
    "he": re.compile(r"[\u0590-\u05ff]"),
    "hi": re.compile(r"[\u0900-\u097f]"),
    "th": re.compile(r"[\u0e00-\u0e7f]"),
    "el": re.compile(r"[\u0370-\u03ff]"),
    "bn": re.compile(r"[\u0980-\u09ff]"),
    "ta": re.compile(r"[\u0b80-\u0bff]"),
}
_LATIN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]")
_CHAR_HINTS: tuple[tuple[str, re.Pattern], ...] = (
    ("pl", re.compile(r"[łŁąĄęĘśŚżŻźŹćĆńŃ]")),
    ("cs", re.compile(r"[řŘěĚůŮ]")),
    ("tr", re.compile(r"[ğĞıİşŞ]")),
    ("vi", re.compile(r"[ăâđêôơưĂÂĐÊÔƠƯ]")),
    ("es", re.compile(r"[ñÑ¿¡]")),
)


def _latin_language(text: str) -> str:
    words = re.findall(r"[^\W\d_]+", text.lower(), re.UNICODE)
    if len(words) < 4:
        return ""
    scores: dict[str, int] = {}
    for lang, stopwords in _LATIN_STOPWORDS.items():
        score = sum(1 for word in words if word in stopwords)
        if score:
            scores[lang] = score
    if not scores:
        return ""
    best_lang, best_score = max(scores.items(), key=lambda item: item[1])
    second = max((score for lang, score in scores.items() if lang != best_lang), default=0)
    if best_score < 2 or best_score <= second:
        return ""
    for lang, pattern in _CHAR_HINTS:
        if pattern.search(text):
            scores[lang] = scores.get(lang, 0) + 2
            break
    best_lang, best_score = max(scores.items(), key=lambda item: item[1])
    second = max((score for lang, score in scores.items() if lang != best_lang), default=0)
    if best_score < 2 or best_score <= second:
        return ""
    return best_lang


def detect_language(text: str, html_lang: str = "") -> str:
    """识别语言代码：优先规范化 html_lang，否则按文字脚本与停用词判断。"""
    lang = normalize_lang_code(html_lang)
    if lang:
        return lang
    value = text if isinstance(text, str) else ""
    if not value:
        return ""
    counts = {
        code: len(pattern.findall(value))
        for code, pattern in _SCRIPT_PATTERNS.items()
    }
    latin = len(_LATIN_RE.findall(value))
    kana = counts.get("ja", 0)
    han = counts.get("zh", 0)
    hangul = counts.get("ko", 0)
    if kana >= 2 and kana + han >= latin:
        return "ja"
    if hangul >= 2 and hangul >= latin:
        return "ko"
    if han >= 3 and han >= latin:
        return "zh"
    if counts.get("ru", 0) >= 3 and counts["ru"] >= latin:
        return "ru"
    for code in ("ar", "he", "hi", "th", "el", "bn", "ta"):
        if counts.get(code, 0) >= 2 and counts[code] >= latin:
            return code
    if latin >= 4:
        return _latin_language(value)
    return ""
