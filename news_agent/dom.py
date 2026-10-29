"""DOM 解析与选择器工具。

所有“从网页里取字段”的动作都走这里，保证 discover / extract 使用同一套语义：

- 选择器语法：
  - CSS 子集：标签、`*`、`.class`、`#id`、`[attr]`、`[attr=value]`、
    `[attr~=value]`、`[attr^=value]`、`[attr$=value]`、`[attr*=value]`、
    `:first-child`、`:last-child`、`:nth-child(n)`、后代/子代/相邻/兄弟组合符、逗号分组
  - XPath：以 `xpath:`、`//` 或 `(/` 开头的表达式
- 取值后缀：`::text`（默认）、`::attr(href)`、`::html`
- 选择器作用域始终是传入节点（内部的 `//` 会转换为 `.//`），
  因此可以安全地对文章正文容器再做二次选择。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from lxml import etree
from lxml import html as lxml_html

_META_KEYS = ("property", "name", "itemprop")

BOILERPLATE_TAGS = (
    "script", "style", "noscript", "svg", "iframe", "form", "nav", "footer",
    "header", "aside", "button", "input", "select", "textarea", "template",
    "canvas", "video", "audio",
)
BOILERPLATE_MARKERS = (
    "advert", "ads-", "ads_", "ad-", "ad_", "related", "share", "social",
    "newsletter", "cookie", "subscribe", "promo", "banner", "sidebar",
    "comment", "recommend", "toolbar", "breadcrumb",
)

_EXTRACTOR_RE = re.compile(
    r"::(attr)\(([^)]*)\)\s*$|::(text|html)(?:\(\))?\s*$", re.IGNORECASE
)
_ATTR_RE = re.compile(r"^\s*([\w:.-]+)\s*(?:([~^$*|]?=)\s*(.*))?$", re.S)
_SIMPLE_RE = re.compile(r"^([*A-Za-z][\w-]*)|^(\*)")
_CLASS_RE = re.compile(r"\.([\w-]+)")
_ID_RE = re.compile(r"#([\w-]+)")
_TAG_RE = re.compile(r"^[A-Za-z][\w-]*")
_ATTR_BLOCK_RE = re.compile(r"\[([^\]]*)\]")
_PSEUDO_RE = re.compile(r":([\w-]+)(?:\(([^)]*)\))?")
_SIMPLE_VALID_RE = re.compile(
    r"^(?:\*|[A-Za-z][\w-]*)?(?:[.#][\w-]+|\[[^\]]*\]|:[\w-]+(?:\([^)]*\))?)*$"
)


# ---------------------------------------------------------------------------
# HTML 解析
# ---------------------------------------------------------------------------

def parse_html(content: str | bytes) -> etree._Element:
    """把整页 HTML 解析成 lxml 元素树根节点。"""
    if content is None:
        raise ValueError("HTML 内容为空")
    if isinstance(content, bytes):
        return lxml_html.document_fromstring(content)
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", content)
    if not text.strip():
        raise ValueError("HTML 内容为空")
    parser = lxml_html.HTMLParser(encoding="utf-8", recover=True)
    try:
        return lxml_html.document_fromstring(text, parser=parser)
    except etree.ParserError as error:
        raise ValueError(f"HTML 解析失败：{error}") from error


# ---------------------------------------------------------------------------
# CSS 子集 -> XPath
# ---------------------------------------------------------------------------

def _xpath_literal(value: str) -> str:
    """生成安全的 XPath 字符串字面量（含单双引号都兼容）。"""
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    pieces = value.split("'")
    literals = ["concat("]
    for index, piece in enumerate(pieces):
        if index:
            literals.append("\"'\", ")
        literals.append(f"'{piece}', ")
    literals[-1] = literals[-1].rstrip(", ") + ")"
    return "".join(literals)


def _split_top_level(value: str, sep: str) -> list[str]:
    """按顶层分隔符切分，忽略引号、方括号、圆括号内部的分隔符。"""
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    quote = ""
    for char in value:
        if quote:
            buffer.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
            buffer.append(char)
            continue
        if char in "([":
            depth += 1
            buffer.append(char)
            continue
        if char in ")]":
            depth = max(0, depth - 1)
            buffer.append(char)
            continue
        if char == sep and depth == 0:
            parts.append("".join(buffer))
            buffer = []
            continue
        buffer.append(char)
    parts.append("".join(buffer))
    return [part.strip() for part in parts if part.strip()]


def _tokenize_group(group: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    current: list[str] = []
    index, length = 0, len(group)
    while index < length:
        char = group[index]
        if char.isspace():
            if current:
                tokens.append(("simple", "".join(current)))
                current = []
            while index < length and group[index].isspace():
                index += 1
            tokens.append(("combinator", " "))
            continue
        if char in ">+~":
            if current:
                tokens.append(("simple", "".join(current)))
                current = []
            tokens.append(("combinator", char))
            index += 1
            continue
        if char == "[":
            end = group.find("]", index)
            if end == -1:
                current.append(group[index:])
                break
            current.append(group[index:end + 1])
            index = end + 1
            continue
        if char == ":":
            match = _PSEUDO_RE.match(group, index)
            if match:
                current.append(match.group(0))
                index = match.end()
                continue
        current.append(char)
        index += 1
    if current:
        tokens.append(("simple", "".join(current)))
    return tokens


def _attr_predicate(content: str) -> str:
    match = _ATTR_RE.match(content)
    if not match:
        return "1=1"
    name, operator, raw = match.group(1), match.group(2), (match.group(3) or "").strip()
    if not operator:
        return f"@{name}"
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        raw = raw[1:-1]
    literal = _xpath_literal(raw)
    if operator == "=":
        return f"@{name}={literal}"
    if operator == "~=":
        return f"contains(concat(' ', normalize-space(@{name}), ' '), {_xpath_literal(' ' + raw + ' ')})"
    if operator == "^=":
        return f"starts-with(@{name}, {literal})"
    if operator == "$=":
        return f"substring(@{name}, string-length(@{name}) - {max(0, len(raw) - 1)})={literal}"
    if operator == "*=":
        return f"contains(@{name}, {literal})"
    if operator == "|=":
        return f"(@{name}={literal} or starts-with(@{name}, {_xpath_literal(raw + '-')}))"
    return f"@{name}"


def _simple_to_xpath(simple: str) -> str:
    # 选择器写错时直接判为无效（select_nodes 会捕获并返回空），避免退化成宽匹配。
    if not _SIMPLE_VALID_RE.match(simple):
        raise ValueError(f"无效的 CSS 选择器片段：{simple!r}")
    tag_match = _TAG_RE.match(simple)
    tag = tag_match.group(0) if tag_match else "*"
    predicates: list[str] = []
    consumed = tag_match.end() if tag_match else (1 if simple.startswith("*") else 0)
    rest = simple[consumed:]
    # 先取出属性条件，再从剩余部分扫描 class/id/伪类，避免把属性值里的 ".html" 当成 class。
    attribute_blocks = _ATTR_BLOCK_RE.findall(rest)
    scan = _ATTR_BLOCK_RE.sub("", rest)
    for class_name in _CLASS_RE.findall(scan):
        predicates.append(
            "contains(concat(' ', normalize-space(@class), ' '), "
            + _xpath_literal(f" {class_name} ") + ")"
        )
    for element_id in _ID_RE.findall(scan):
        predicates.append(f"@id={_xpath_literal(element_id)}")
    for content in attribute_blocks:
        predicates.append(_attr_predicate(content))
    for name, argument in _PSEUDO_RE.findall(scan):
        name = name.lower()
        if name == "first-child":
            predicates.append("position() = 1")
        elif name == "last-child":
            predicates.append("position() = last()")
        elif name == "nth-child" and argument and argument.strip().isdigit():
            predicates.append(f"position() = {int(argument.strip())}")
        # :not(...) 等复杂伪类暂不支持，忽略该条件（宁宽勿错）。
    suffix = "".join(f"[{predicate}]" for predicate in predicates)
    return tag + suffix


def _group_to_xpath(group: str) -> str:
    tokens = _tokenize_group(group.strip())
    result = ""
    first = True
    pending = " "
    explicit = False
    for kind, value in tokens:
        if kind == "combinator":
            if value in ">+~":
                pending = value
                explicit = True
            elif not explicit:
                # 纯空白只是后代选择器的分隔；若紧跟显式组合符则不能覆盖它。
                pending = " "
            continue
        step = _simple_to_xpath(value)
        if first:
            result = "//" + step
            first = False
        elif pending == ">":
            result += "/" + step
        elif pending == "+":
            result += "/following-sibling::*[1]/self::" + step
        elif pending == "~":
            result += "/following-sibling::" + step
        else:
            result += "//" + step
        pending = " "
        explicit = False
    return result


def css_to_xpath(selector: str) -> str:
    """CSS 子集 -> XPath 1.0 表达式。"""
    groups = _split_top_level(selector, ",")
    return " | ".join(_group_to_xpath(group) for group in groups if group.strip())


def _subtree_expression(selector: str) -> str:
    value = selector.strip()
    if value.lower().startswith("xpath:"):
        value = value[6:].strip()
    elif not (value.startswith("//") or value.startswith("/") or value.startswith(".//") or value.startswith("(")):
        value = css_to_xpath(value)
    parts = _split_top_level(value, "|")
    normalized = []
    for part in parts:
        part = part.strip()
        if part.startswith(".//"):
            normalized.append(part)
        elif part.startswith("//"):
            normalized.append("." + part)
        else:
            normalized.append(part)
    return " | ".join(normalized) if normalized else "(.//*)"


# ---------------------------------------------------------------------------
# 节点选择与取值
# ---------------------------------------------------------------------------

def _split_extractor(selector: str) -> tuple[str, str, str]:
    match = _EXTRACTOR_RE.search(selector)
    if not match:
        return selector.strip(), "text", ""
    base = selector[: match.start()].strip()
    if match.group(1):  # ::attr(name)
        return base, "attr", match.group(2).strip()
    return base, match.group(3).lower(), ""  # ::text / ::html / ::text() / ::html()


def select_nodes(root: etree._Element | None, selector: str) -> list[etree._Element]:
    """返回匹配的节点列表；选择器写错时返回空列表而不是抛异常。"""
    if root is None or not selector or not str(selector).strip():
        return []
    base, _kind, _argument = _split_extractor(str(selector))
    if not base:
        return []
    try:
        expression = _subtree_expression(base)
        nodes = root.xpath(expression)
    except (etree.XPathError, ValueError, TypeError):
        return []
    return [node for node in nodes if isinstance(node, etree._Element)]


def _inner_html(node: etree._Element) -> str:
    pieces = [etree.tostring(child, encoding="unicode", method="html") for child in node]
    return "".join(pieces)


def select_values(
    root: etree._Element | None,
    selector: str,
    *,
    drop_empty: bool = True,
) -> list[str]:
    """按选择器取值：默认取文本，可用 ::attr(name) / ::html 后缀改变取值方式。"""
    if root is None or not selector or not str(selector).strip():
        return []
    base, kind, argument = _split_extractor(str(selector))
    nodes = select_nodes(root, base)
    values: list[str] = []
    for node in nodes:
        if kind == "attr":
            value = node.get(argument) or ""
            value = value.strip()
        elif kind == "html":
            value = _inner_html(node).strip()
        else:
            value = node_text(node)
        if value or not drop_empty:
            values.append(value)
    return values


def first_value(root: etree._Element | None, selector: str) -> str:
    values = select_values(root, selector)
    return values[0] if values else ""


def node_text(node: etree._Element | None) -> str:
    if node is None:
        return ""
    return clean_ws("".join(node.itertext()))


def clean_ws(text: str) -> str:
    """压缩空白，保留普通空格语义。"""
    if not text:
        return ""
    return " ".join(str(text).replace("\u00a0", " ").replace("\u200b", "").split())


def container_text(node: etree._Element | None, sep: str = "\n") -> str:
    """抽取容器内可读文本：块级元素之间换行，去掉脚本/导航/广告等噪声。"""
    if node is None:
        return ""
    from copy import deepcopy

    clone = deepcopy(node)
    remove_boilerplate(clone)
    pieces: list[str] = []

    def walk(element: etree._Element, is_root: bool = False) -> None:
        tag = element.tag if isinstance(element.tag, str) else ""
        if element.text:
            pieces.append(element.text)
        for child in element:
            child_tag = child.tag if isinstance(child.tag, str) else ""
            if child_tag in ("br",):
                pieces.append(sep)
            elif child_tag in ("p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "td", "th", "section", "article"):
                pieces.append(sep)
                walk(child)
                pieces.append(sep)
            else:
                walk(child)
            if child.tail:
                pieces.append(child.tail)

    walk(clone)
    text = "".join(pieces)
    lines = [clean_ws(line) for line in text.splitlines()]
    return sep.join(line for line in lines if line)


def remove_boilerplate(node: etree._Element, extra_markers: Iterable[str] = ()) -> None:
    """就地删除脚本/导航/广告/相关推荐等噪声节点。"""
    markers = tuple(BOILERPLATE_MARKERS) + tuple(extra_markers)
    for tag in BOILERPLATE_TAGS:
        for element in node.xpath(f".//{tag}"):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
    for element in node.xpath(".//*[@class or @id]"):
        marker_text = f"{element.get('class', '')} {element.get('id', '')}".lower()
        if any(marker in marker_text for marker in markers):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)


# ---------------------------------------------------------------------------
# meta / JSON-LD
# ---------------------------------------------------------------------------

def meta_content(root: etree._Element | None, *names: str) -> str:
    """读取 meta[property|name|itemprop] 的 content；names 不区分大小写。"""
    if root is None or not names:
        return ""
    wanted = {name.strip().lower() for name in names if name}
    for meta in root.xpath("//meta"):
        key = ""
        for attribute in _META_KEYS:
            value = meta.get(attribute)
            if value:
                key = value.strip().lower()
                break
        if key in wanted:
            content = (meta.get("content") or "").strip()
            if content:
                return content
    return ""


def meta_content_by_prefix(root: etree._Element | None, *prefixes: str) -> str:
    """按属性前缀读取 meta（例如 article: / og:）。"""
    if root is None:
        return ""
    wanted = tuple(prefix.lower() for prefix in prefixes)
    for meta in root.xpath("//meta"):
        for attribute in _META_KEYS:
            value = meta.get(attribute)
            if value and value.strip().lower().startswith(wanted):
                content = (meta.get("content") or "").strip()
                if content:
                    return content
    return ""


def _flatten_json_ld(value: Any, bucket: list[dict[str, Any]]) -> None:
    if isinstance(value, list):
        for item in value:
            _flatten_json_ld(item, bucket)
    elif isinstance(value, dict):
        bucket.append(value)
        if "@graph" in value:
            _flatten_json_ld(value.get("@graph"), bucket)


def json_ld_objects(root: etree._Element | None) -> list[dict[str, Any]]:
    """解析页面全部 JSON-LD script，展开 @graph，忽略坏数据。"""
    if root is None:
        return []
    bucket: list[dict[str, Any]] = []
    scripts = root.xpath(
        '//script[contains(translate(@type, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", '
        '"abcdefghijklmnopqrstuvwxyz"), "ld+json")]/text()'
    )
    for raw in scripts:
        text = (raw or "").strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # 有些站点内联了非法 JSON-LD（尾逗号等），交给通用抽取兜底。
            continue
        _flatten_json_ld(data, bucket)
    return bucket


def _type_names(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value.strip().lower()}
    if isinstance(value, list):
        names: set[str] = set()
        for item in value:
            names.update(_type_names(item))
        return names
    return set()


def json_ld_by_type(
    root: etree._Element | None,
    types: Iterable[str],
) -> list[dict[str, Any]]:
    """按 @type 过滤 JSON-LD 对象（如 NewsArticle / ItemList / Organization）。"""
    wanted = {item.strip().lower() for item in types}
    return [
        item
        for item in json_ld_objects(root)
        if _type_names(item.get("@type")) & wanted
    ]


def first_json_ld(root: etree._Element | None, types: Iterable[str]) -> dict[str, Any]:
    items = json_ld_by_type(root, types)
    return items[0] if items else {}


def json_ld_text(value: Any) -> str:
    """JSON-LD 字段常见形态归一：str / {"@value"} / {"name"} / list。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return clean_ws(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("name", "@value", "headline", "text"):
            if key in value:
                text = json_ld_text(value[key])
                if text:
                    return text
        return ""
    if isinstance(value, list):
        for item in value:
            text = json_ld_text(item)
            if text:
                return text
    return ""
