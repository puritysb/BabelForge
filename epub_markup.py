"""Safe inline XHTML shared by extraction, translation and EPUB assembly."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from lxml import etree, html

XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
XML_NS = "http://www.w3.org/XML/1998/namespace"
INLINE_TAGS = frozenset({
    "a", "abbr", "b", "bdi", "bdo", "br", "cite", "code", "del", "dfn",
    "em", "i", "img", "ins", "kbd", "mark", "q", "rp", "rt", "ruby",
    "s", "samp", "small", "span", "strong", "sub", "sup", "time", "u", "var",
})
_DROP_TAGS = {"script", "style", "iframe", "object", "embed", "form"}
_ATTRS = {"id", "class", "title", "lang", "dir", "href", "src", "alt",
          "width", "height", "role", "epub:type", "xml:lang", "datetime"}
_TAG_RE = re.compile(
    r"</?[A-Za-z][A-Za-z0-9:-]*(?:\s+(?:[^<>\"']|\"[^\"]*\"|'[^']*')*)?\s*/?>"
)
_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def safe_url(value: str) -> bool:
    """Allow packaged references and ordinary hyperlinks, never active URLs."""
    value = value.strip()
    if any(ord(ch) < 32 for ch in value):
        return False
    return urlsplit(value).scheme.lower() in {"", "http", "https", "mailto"}


def _fragment(text: str):
    root = html.fragment_fromstring(_INVALID_XML.sub("", str(text or "")),
                                    create_parent="div")
    # HTML's forgiving parser retains XML prefixes as literal tag names.
    # Normalize these before QName/allowlist processing; the final inline
    # allowlist still rejects foreign or active elements.
    for node in root.iterdescendants():
        if isinstance(node.tag, str) and ":" in node.tag:
            node.tag = node.tag.rsplit(":", 1)[1]
    return root


def _attribute_name(node, name: str) -> tuple[str, str]:
    """Resolve EPUB/XML attributes even when the input uses another prefix."""
    if name.startswith("{"):
        qname = etree.QName(name)
        namespace, local = qname.namespace, qname.localname
    elif ":" in name:
        prefix, local = name.split(":", 1)
        namespace = {"xml": XML_NS, "epub": EPUB_NS}.get(prefix)
        for ancestor in (node, *node.iterancestors()):
            declared = ancestor.get(f"xmlns:{prefix}")
            if declared:
                namespace = declared
                break
    else:
        return name, name
    if namespace == XML_NS and local == "lang":
        return "xml:lang", f"{{{XML_NS}}}lang"
    if namespace == EPUB_NS and local == "type":
        return "epub:type", f"{{{EPUB_NS}}}type"
    return name, name


def plain_text(text: str) -> str:
    root = _fragment(text)
    for node in list(root.iterdescendants()):
        if isinstance(node.tag, str) and node.tag.lower() in _DROP_TAGS:
            node.drop_tree()
        elif isinstance(node.tag, str) and node.tag.lower() in {"br", "img"}:
            node.tail = " " + (node.tail or "")
    return re.sub(r"\s+", " ", "".join(root.itertext())).strip()


def sanitize_inline(text: str) -> str:
    """Keep semantic inline markup and its text, serialized as valid XHTML.

    Input may also be plain text; entities are decoded once by the HTML parser.
    Unrecognized wrappers are unwrapped, while active content is discarded.
    """
    root = _fragment(text)
    for node in reversed(list(root.iterdescendants())):
        if not isinstance(node.tag, str):
            node.drop_tree()
            continue
        tag = etree.QName(node).localname.lower()
        if tag in _DROP_TAGS:
            node.drop_tree()
            continue
        if tag not in INLINE_TAGS:
            node.drop_tag()
            continue
        node.tag = tag
        attributes = {}
        for key, value in node.attrib.items():
            allowed_key, output_key = _attribute_name(node, key)
            if allowed_key in _ATTRS and (allowed_key not in {"href", "src"}
                                         or safe_url(value)):
                attributes[output_key] = value
        node.attrib.clear()
        for key, value in attributes.items():
            node.set(key, value)
    serialized = etree.tostring(root, encoding="unicode", method="xml")
    return serialized[serialized.find(">") + 1:serialized.rfind("</div>")] if len(root) or root.text else ""


def stash_inline_tags(text: str, prefix: str = "T") -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}
    def replace(match):
        marker = f"◀{prefix}TAG{len(mapping)}▶"
        if marker in text:
            raise ValueError("source contains reserved inline placeholder")
        mapping[marker] = match.group(0)
        return marker
    return _TAG_RE.sub(replace, text), mapping


def restore_inline_tags(text: str, mapping: dict[str, str]) -> str:
    for marker, tag in mapping.items():
        text = text.replace(marker, tag)
    return text


def placeholders_preserved(text: str, mapping: dict[str, str]) -> bool:
    positions = [text.find(marker) for marker in mapping]
    return (all(text.count(marker) == 1 for marker in mapping)
            and positions == sorted(positions)
            and set(re.findall(r"◀[^◀▶]*TAG\d+▶", text)) == set(mapping))
