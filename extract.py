"""Extract a per-chapter paragraph list from a fetched source.

Input: SourceText (path to .epub or .txt)
Output: list[Chapter(title, paragraphs=[str, ...])]

Cleaning rules:
- Strip Project Gutenberg license headers/footers (*** START OF / *** END OF)
- Drop empty paragraphs and pure-boilerplate lines
- Preserve chapter/TOC structure so assemble.py can rebuild spine
"""
from __future__ import annotations
import copy
import hashlib
import posixpath
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlsplit
from xml.sax.saxutils import escape

from lxml import etree
from sources import SourceText
import config


# Sentence splitter for SENTENCE_LEVEL_SPLIT mode. Splits on Latin . ! ? OR
# CJK 。！？followed by whitespace + a capital letter / quote (Latin scripts),
# or just on 。！？ for Japanese/CJK where there's no capitalization cue.
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-Z"\'\[\(])|(?<=[。！？])\s*')
_ABBREVIATIONS = (
    "Mr.", "Mrs.", "Ms.", "Dr.", "Prof.", "Sr.", "Jr.", "St.", "vs.",
    "etc.", "i.e.", "e.g.", "cf.", "pp.", "Vol.", "No.", "Ch.", "Aug.",
    "Sept.", "Oct.", "Nov.", "Dec.",
)


def split_sentences(text: str) -> list[str]:
    """Split a paragraph into sentences. Returns the original paragraph as a
    single element if it's already short (≤1 sentence)."""
    if not config.SENTENCE_LEVEL_SPLIT:
        return [text]
    masked = text
    for abbr in _ABBREVIATIONS:
        masked = masked.replace(abbr, abbr.replace(".", "\x00"))
    parts = _SENTENCE_SPLIT.split(masked)
    parts = [p.replace("\x00", ".").strip() for p in parts]
    parts = [p for p in parts if p]
    return parts or [text]


def _paragraphs_to_units(paragraphs: list[str]) -> list[str]:
    """Turn raw paragraphs into the bilingual pair units we'll translate.

    With SENTENCE_LEVEL_SPLIT, each unit is a sentence (short alternating
    pairs in the reader). Without it, each unit is the paragraph as-is.
    """
    units: list[str] = []
    for p in paragraphs:
        units.extend(split_sentences(p))
    return units


@dataclass
class Chapter:
    title: str
    # Every translation unit is an inline XHTML fragment, including escaped TXT.
    paragraphs: list[str] = field(default_factory=list)
    template: str | None = None
    assets: dict[str, bytes] = field(default_factory=dict)


# Gutenberg's "*** START OF THE PROJECT GUTENBERG EBOOK X ***" / "*** END OF ..."
GUTEN_START = re.compile(r"\*{3}\s*START\s+OF.*?\*{3}", re.IGNORECASE | re.DOTALL)
GUTEN_END = re.compile(r"\*{3}\s*END\s+OF.*?\*{3}", re.IGNORECASE | re.DOTALL)
# Credits block that follows the START marker (often duplicated PG boilerplate).
GUTEN_PRODUCED = re.compile(
    r"^\s*Produced by .*?(?:pgdp\.net|project gutenberg|distributed proofreading)"
    r"[^\n]*\n(?:[^\n]+\n){0,6}?",
    re.IGNORECASE | re.MULTILINE,
)

# Chapter heading patterns. Order matters — most specific first.
CHAPTER_RE = re.compile(
    r"^\s*("
    # "CHAPTER I", "Chapter 1", "Ch. V" — classical novels
    r"(?:CHAPTER|Chapter|Chap\.?|Ch\.)\s+[IVXLC0-9]+\b"
    r"|第\s*[一二三四五六七八九十百千0-9]+\s*章"
    r"|제\s*[0-9一二三四五六七八九十]+\s*장"
    # Japanese chapter headings: 【一】, 【 二 】, 第1章, 第一章, or standalone
    # 一/二/.../十一 inside 【】 brackets (Wikisource/Aozora convention)
    r"|【\s*[一二三四五六七八九十百千0-9]+\s*】"
    # All-caps section titles, 2-6 words (Nietzsche: "OF THE FIRST AND LAST THINGS",
    # "THE RELIGIOUS LIFE", "AUTHOR'S PREFACE"). Must be mostly uppercase letters
    # and short — long sentences in caps are not headings.
    r"|[A-Z][A-Z'\-]{1,}(?:\s+[A-Z'\-]+){1,5}\.?"
    r")\s*$",
    re.MULTILINE,
)

TOC_LABEL_RE = re.compile(
    r"^(?:table\s+of\s+contents|contents|inhalt|sommaire|"
    r"(?:í|i)ndice|目次)\s*\.?$",
    re.IGNORECASE,
)


def _toc_headings(raw: str) -> set[str]:
    """Return normalized headings listed in an indented plain-text TOC.

    Gutenberg TXT files often preserve a useful multilingual contents block
    even when their real section headings are title case rather than ``CHAPTER
    I`` or all caps.  Nietzsche 7207 is one such file: the old detector found
    no headings, collapsed the whole book to ``Untitled``, and the device then
    displayed 37 meaningless safety chunks.  Treating only entries copied from
    an explicit indented TOC as headings avoids guessing that short aphorisms
    are chapters.
    """
    lines = raw.splitlines()
    headings: set[str] = set()
    for index, line in enumerate(lines):
        if not TOC_LABEL_RE.fullmatch(line.strip()):
            continue
        cursor = index + 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        block: list[str] = []
        while cursor < len(lines):
            candidate = lines[cursor]
            if not candidate.strip():
                break
            if candidate[:1] not in {" ", "\t"}:
                break
            value = re.sub(r"\s+", " ", candidate).strip()
            if value:
                block.append(value)
            cursor += 1
        # A single indented line is too ambiguous (it may be an epigraph).
        if len(block) >= 2:
            headings.update(_heading_key(value) for value in block)
    return headings


def _heading_key(value: str) -> str:
    return re.sub(r"[.。:：]+$", "", re.sub(r"\s+", " ", value).strip()).casefold()

def _remove_explicit_toc(raw: str) -> str:
    """Remove only a recognized, indented contents list that we rebuild later."""
    lines = raw.splitlines(keepends=True)
    remove: set[int] = set()
    for index, line in enumerate(lines):
        if not TOC_LABEL_RE.fullmatch(line.strip()):
            continue
        cursor = index + 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        first = cursor
        while cursor < len(lines) and lines[cursor].strip() and lines[cursor][:1] in {" ", "\t"}:
            cursor += 1
        if cursor - first >= 2:
            remove.update(range(index, cursor))
    return "".join(line for index, line in enumerate(lines) if index not in remove)


def extract(src: SourceText) -> list[Chapter]:
    if src.format == "epub":
        return _extract_epub(src.path)
    if src.format == "txt":
        return _extract_txt(src.path)
    if src.format == "html":
        return _extract_html(src)
    raise ValueError(f"unsupported source format: {src.format}")


XHTML_NS = "http://www.w3.org/1999/xhtml"
_BLOCKS = {"body", "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li",
           "blockquote", "section", "article", "td", "th", "dd", "dt", "pre",
           "figcaption", "caption", "aside", "header", "footer", "ul", "ol",
           "dl", "table", "thead", "tbody", "tfoot", "tr", "figure", "main",
           "nav", "address"}
_OPAQUE = {"svg", "math", "audio", "video", "img"}
_UNSAFE = {"script", "iframe", "object", "embed", "form", "input", "button",
           "select", "textarea", "style", "link", "base"}


def _local_name(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return (etree.QName(tag).localname if tag.startswith("{") else tag.rsplit(":", 1)[-1]).lower()


def _document_body(data: bytes, *, html: bool = False):
    parser = etree.HTMLParser(no_network=True) if html else etree.XMLParser(
        resolve_entities=False, no_network=True, recover=False)
    root = etree.fromstring(data, parser)
    if root is None:
        raise ValueError("source document has no content")
    bodies = root.xpath("//*[local-name()='body']")
    if not bodies:
        raise ValueError("source document has no body")
    body = copy.deepcopy(bodies[0])
    for node in list(body.iter()):
        if not isinstance(node.tag, str):
            parent = node.getparent()
            if parent is not None:
                tail = node.tail or ""
                previous = node.getprevious()
                if previous is not None:
                    previous.tail = (previous.tail or "") + tail
                else:
                    parent.text = (parent.text or "") + tail
                parent.remove(node)
            continue
        local = _local_name(node.tag)
        if local in _UNSAFE:
            parent = node.getparent()
            if parent is not None:
                # Preserve text following active elements, but never their code.
                tail = node.tail or ""
                previous = node.getprevious()
                if previous is not None:
                    previous.tail = (previous.tail or "") + tail
                else:
                    parent.text = (parent.text or "") + tail
                parent.remove(node)
            continue
        if not node.tag.startswith("{"):
            node.tag = f"{{{XHTML_NS}}}{local}"
        if "srcset" in node.attrib:
            parent = node.getparent()
            if node.get("src"):
                # Keep the packaged fallback image; responsive alternatives
                # otherwise need a second URL grammar and manifest treatment.
                del node.attrib["srcset"]
            elif (local == "source" and parent is not None
                  and _local_name(parent.tag) == "picture"
                  and any(_local_name(child.tag) == "img" and child.get("src") for child in parent)):
                parent.remove(node)
                continue
            else:
                raise ValueError("source has responsive media without a packaged src fallback")
        for attribute in list(node.attrib):
            name = _local_name(attribute)
            value = node.attrib[attribute].strip()
            if (name.startswith("on") or name.startswith("data-bf-") or name == "style"
                    or (name in {"href", "src", "action"} and
                        urlsplit(value).scheme.lower() in {"javascript", "vbscript", "file"})):
                del node.attrib[attribute]
            elif ":" in attribute and not attribute.startswith("{"):
                prefix, name = attribute.split(":", 1)
                namespace = {"xml": "http://www.w3.org/XML/1998/namespace",
                             "epub": "http://www.idpf.org/2007/ops",
                             "xlink": "http://www.w3.org/1999/xlink"}.get(prefix)
                del node.attrib[attribute]
                if namespace:
                    node.set(f"{{{namespace}}}{name}", value)
    return body


def _inline_content(node) -> str:
    parts = [node.text or ""]
    # Text needs XML escaping, child serialization already supplies it.
    parts[0] = escape(parts[0])
    parts.extend(etree.tostring(child, encoding="unicode", with_tail=True)
                 for child in node)
    return "".join(parts)


def _bounded_text_chunks(text: str, budget: int, sentence_split: bool = False) -> list[str]:
    """Split without discarding a single character, preferring natural breaks."""
    segments = [text]
    if sentence_split:
        masked = text
        for abbreviation in _ABBREVIATIONS:
            masked = masked.replace(abbreviation, abbreviation.replace(".", "\x00"))
        ends = [match.end() for match in _SENTENCE_SPLIT.finditer(masked)] + [len(text)]
        segments = []
        start = 0
        for end in ends:
            if end > start:
                segments.append(text[start:end])
                start = end
    chunks = []
    for segment in segments:
        while len(escape(segment)) > budget:
            low, high = 1, min(len(segment), budget)
            while low < high:
                middle = (low + high + 1) // 2
                if len(escape(segment[:middle])) <= budget:
                    low = middle
                else:
                    high = middle - 1
            limit = low
            prefix = segment[:limit]
            # Forced budget splits need the same abbreviation protection as
            # optional sentence splitting. Otherwise a long paragraph can end
            # at Mr./Mrs. and leave its name/relative clause in the next slot.
            masked_prefix = prefix
            for abbreviation in _ABBREVIATIONS:
                masked_prefix = masked_prefix.replace(
                    abbreviation, abbreviation.replace(".", "\x00"))
            sentence_breaks = list(re.finditer(
                r"[.!?。！？](?:\s+|(?=[^\x00-\x7f]))", masked_prefix))
            whitespace = [match for match in re.finditer(r"\s+", masked_prefix)
                          if match.start() == 0 or masked_prefix[match.start() - 1] != "\x00"]
            boundary = (sentence_breaks[-1].end() if sentence_breaks else
                        whitespace[-1].end() if whitespace else limit)
            chunks.append(segment[:boundary])
            segment = segment[boundary:]
        if segment:
            chunks.append(segment)
    return chunks


def _mark_units(body, *, sentence_split: bool = False) -> list[str]:
    """Partition visible source text once, keeping the surrounding XHTML tree."""
    paragraphs: list[str] = []
    budget = max(1, int(config.TRANSLATE_BATCH_MAX_CHARS))

    def has_blocks(node):
        return any(_local_name(child.tag) in _BLOCKS | _OPAQUE
                   for child in node.iterdescendants())

    def bounded_text(parent, text):
        for chunk in _bounded_text_chunks(text, budget, sentence_split):
            if chunk.strip():
                unit = etree.SubElement(parent, f"{{{XHTML_NS}}}span")
                unit.text = chunk
                unit.set("data-bf-unit", str(len(paragraphs)))
                paragraphs.append(escape(chunk))
            elif len(parent):
                parent[-1].tail = (parent[-1].tail or "") + chunk
            else:
                parent.text = (parent.text or "") + chunk

    def partition(node):
        """Keep semantic inline wrappers in place while bounding their text."""
        if _local_name(node.tag) in _OPAQUE:
            return
        children = list(node)
        text = node.text or ""
        node.text = None
        for child in children:
            node.remove(child)
        bounded_text(node, text)
        for child in children:
            tail = child.tail or ""
            child.tail = None
            node.append(child)
            partition(child)
            bounded_text(node, tail)

    def mark(node):
        if "".join(node.itertext()).strip():
            content = _inline_content(node)
            if len(content) > budget or sentence_split:
                partition(node)
                return
            node.set("data-bf-unit", str(len(paragraphs)))
            paragraphs.append(content)

    def visit(node):
        if _local_name(node.tag) in _OPAQUE:
            return
        if not has_blocks(node) and _local_name(node.tag) not in {"body", "ul", "ol", "table", "tr", "dl"}:
            mark(node)
            return
        children = list(node)
        text = node.text or ""
        node.text = None
        for child in children:
            node.remove(child)
        group = etree.Element(f"{{{XHTML_NS}}}span")
        group.text = text

        def flush():
            nonlocal group
            if "".join(group.itertext()).strip():
                node.append(group)
                mark(group)
            else:
                # Even image-only inline runs must remain in the document.
                if len(group):
                    node.append(group)
                elif group.text:
                    if len(node):
                        node[-1].tail = (node[-1].tail or "") + group.text
                    else:
                        node.text = (node.text or "") + group.text
            group = etree.Element(f"{{{XHTML_NS}}}span")

        for child in children:
            tail = child.tail or ""
            child.tail = None
            if _local_name(child.tag) in _BLOCKS or has_blocks(child) or _local_name(child.tag) in _OPAQUE:
                flush()
                node.append(child)
                visit(child)
                group.text = tail
            else:
                group.append(child)
                child.tail = tail
        flush()

    visit(body)
    return paragraphs


def _document_title(body, fallback: str) -> str:
    for level in ("h1", "h2", "h3"):
        nodes = body.xpath(f".//*[local-name()='{level}']")
        if nodes:
            text = " ".join("".join(nodes[0].itertext()).split())
            if text:
                return text[:200]
    return fallback


def _resolve_local(document: str, reference: str) -> tuple[str | None, str]:
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc:
        return None, parsed.fragment
    path = posixpath.normpath(posixpath.join(posixpath.dirname(document), unquote(parsed.path))) if parsed.path else document
    return path, parsed.fragment


def _media_reference(node, attribute: str) -> bool:
    name = _local_name(attribute)
    return name in {"src", "poster"} or (
        name == "href" and _local_name(node.tag) in {"image", "use", "feimage"})


def _extract_epub(path: str) -> list[Chapter]:
    import ebooklib
    from ebooklib import epub
    book = epub.read_epub(path, options={"ignore_ncx": True})
    documents = {item.get_name(): item for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT)}
    # EbookLib consumes the manifest's nav property into the EpubNav type on
    # read, so properties alone misses navigation documents in the spine.
    navigation = {name for name, item in documents.items()
                  if isinstance(item, epub.EpubNav)
                  or "nav" in (getattr(item, "properties", []) or [])}
    selected = []
    for identifier, _linear in book.spine:
        item = book.get_item_with_id(identifier)
        if item is not None and item.get_name() in documents and item.get_name() not in navigation:
            if item.get_name() not in selected:
                selected.append(item.get_name())
    if not selected:
        raise ValueError(f"EPUB has no readable spine documents: {path}")
    bodies = {}
    # Footnotes are often outside the linear spine. Follow local document links
    # recursively so their anchors and backlinks survive the rebuilt package.
    for name in selected:
        body = _document_body(documents[name].get_content())
        bodies[name] = body
        for node in body.iter():
            for attribute, reference in node.attrib.items():
                if _local_name(attribute) != "href":
                    continue
                target, _fragment = _resolve_local(name, reference)
                if target in documents and target not in navigation and target not in selected:
                    selected.append(target)
    document_paths = {name: f"chapter{index}.xhtml" for index, name in enumerate(selected, 1)}
    document_paths.update({name: "nav.xhtml" for name in navigation})
    asset_paths = {}
    assets = {}
    media_items = []
    for item in book.get_items():
        mime = getattr(item, "media_type", "") or ""
        if not mime.startswith(("image/", "font/", "audio/", "video/")) and mime not in {"application/vnd.ms-opentype", "application/font-sfnt", "application/font-woff"}:
            continue
        name = item.get_name()
        basename = re.sub(r"[^A-Za-z0-9._-]", "_", posixpath.basename(name)) or "asset"
        target = f"assets/{hashlib.sha256(name.encode()).hexdigest()[:12]}-{basename}"
        asset_paths[name] = target
        media_items.append(item)
    for item in media_items:
        data = item.get_content()
        name = item.get_name()
        if getattr(item, "media_type", "") == "image/svg+xml":
            tree = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))
            for node in list(tree.iter()):
                if _local_name(node.tag) == "script":
                    parent = node.getparent()
                    if parent is not None:
                        parent.remove(node)
                    continue
                for attribute, reference in list(node.attrib.items()):
                    if _local_name(attribute).startswith("on"):
                        del node.attrib[attribute]
                    elif _local_name(attribute) in {"href", "src"}:
                        if urlsplit(reference).scheme.lower() in {"javascript", "vbscript", "file"}:
                            del node.attrib[attribute]
                            continue
                        target, fragment = _resolve_local(name, reference)
                        if _media_reference(node, attribute) and target not in asset_paths:
                            raise ValueError(f"SVG references missing or external media: {name}: {reference}")
                        if target in asset_paths and not reference.startswith("#"):
                            mapped = posixpath.relpath(asset_paths[target], posixpath.dirname(asset_paths[name]))
                            node.set(attribute, mapped + (f"#{fragment}" if fragment else ""))
            data = etree.tostring(tree, encoding="utf-8", xml_declaration=True)
        assets[asset_paths[name]] = data
    chapters = []
    for name in selected:
        body = bodies[name]
        title = _document_title(body, posixpath.splitext(posixpath.basename(name))[0])
        for node in body.iter():
            for attribute, reference in list(node.attrib.items()):
                if _local_name(attribute) not in {"href", "src", "poster"}:
                    continue
                target, fragment = _resolve_local(name, reference)
                if target is None and _media_reference(node, attribute):
                    raise ValueError(f"EPUB contains unpackaged external media: {name}: {reference}")
                mapped = document_paths.get(target) or asset_paths.get(target)
                if mapped:
                    node.set(attribute, mapped + (f"#{fragment}" if fragment else ""))
                elif target is not None and _media_reference(node, attribute) and reference:
                    raise ValueError(f"EPUB references a missing media resource: {name}: {reference}")
        paragraphs = _mark_units(body, sentence_split=config.SENTENCE_LEVEL_SPLIT)
        chapters.append(Chapter(title=title, paragraphs=paragraphs,
                                template=etree.tostring(body, encoding="unicode"),
                                assets=dict(assets)))
    if not any(chapter.paragraphs for chapter in chapters):
        raise ValueError(f"no text extracted from EPUB: {path}")
    return chapters


def _extract_html(src: SourceText) -> list[Chapter]:
    with open(src.path, "rb") as handle:
        body = _document_body(handle.read(), html=True)
    # A single HTML response is not a complete image-bearing book package.
    # Reject missing media before translation instead of silently losing it.
    for node in body.iter():
        if _local_name(node.tag) in _OPAQUE:
            raise ValueError("standalone HTML contains media; choose the EPUB edition to preserve its resources")
        for attribute, reference in list(node.attrib.items()):
            if _local_name(attribute) not in {"href", "src", "poster"}:
                continue
            if reference and _media_reference(node, attribute):
                raise ValueError("standalone HTML references media; choose the EPUB edition to preserve its resources")
            if reference and not reference.startswith("#") and src.candidate.fetch_url:
                node.set(attribute, urljoin(src.candidate.fetch_url, reference))
    title = _document_title(body, src.candidate.title)
    paragraphs = _mark_units(body, sentence_split=config.SENTENCE_LEVEL_SPLIT)
    if not paragraphs:
        raise ValueError(f"no text extracted from HTML: {src.path}")
    return [Chapter(title=title, paragraphs=paragraphs,
                    template=etree.tostring(body, encoding="unicode"))]


# ─── Plain text ───
def _extract_txt(path: str) -> list[Chapter]:
    with open(path, "r", encoding="utf-8-sig") as f:
        raw = f.read()

    # Slice off the Gutenberg boilerplate if present.
    m = GUTEN_START.search(raw)
    has_gutenberg_start = m is not None
    if m:
        raw = raw[m.end():]
    m = GUTEN_END.search(raw)
    if m:
        raw = raw[:m.start()]
    # Strip the duplicated "Produced by ... pgdp.net" credits block that often
    # immediately follows the START marker.
    if has_gutenberg_start:
        raw = re.sub(r"\A\s*Produced by\b.*?(?=\n\s*\n|\Z)", "", raw,
                     count=1, flags=re.I | re.S)
    raw = GUTEN_PRODUCED.sub("", raw, count=1)
    toc_headings = _toc_headings(raw)
    raw = _remove_explicit_toc(raw)

    # Normalize whitespace into paragraphs (blank-line separated).
    # Collapse runs of whitespace inside a paragraph.
    paras: list[str] = []
    for block in re.split(r"\n\s*\n", raw):
        text = re.sub(r"\s+", " ", block).strip()
        if text:
            paras.append(text)
    if not paras:
        raise ValueError(f"no paragraphs found in txt: {path}")

    # Bucket paragraphs into chapters by heading match. The whole paragraph
    # is the heading (CHAPTER_RE anchors to start+end of a line, but a paragraph
    # may be a single line heading).
    chapters: list[Chapter] = []
    # Use the book's first real heading or fall back to a reader-friendly name.
    # "Front Matter" is publisher jargon; "Preface" / "들어가며" reads better in
    # the device's TOC. The first heading encountered promotes the bucket.
    current = Chapter(title="Preface")
    chapters.append(current)
    source_headings: dict[int, str] = {}
    for p in paras:
        from_toc = _heading_key(p) in toc_headings
        is_heading = bool(CHAPTER_RE.fullmatch(p)) or from_toc
        if is_heading:
            current = Chapter(title=p.strip()[:200])
            source_headings[id(current)] = p.strip()
            chapters.append(current)
        else:
            current.paragraphs.append(p)

    # Drop empty chapters (e.g. trailing Preface with no body).
    chapters = [c for c in chapters if c.paragraphs or id(c) in source_headings]
    if not chapters:
        chapters = [Chapter(title="Untitled", paragraphs=paras)]

    # Keep paragraph grouping in the template even when one long paragraph
    # needs several bounded translation units. The generated heading is the
    # same navigation label used by plain-text assembly. Actual source headings
    # are translated; only the fallback label is synthetic and remains unmarked.
    for chapter in chapters:
        body = etree.Element(f"{{{XHTML_NS}}}body", nsmap={None: XHTML_NS})
        source_heading = source_headings.get(id(chapter))
        if source_heading:
            etree.SubElement(body, f"{{{XHTML_NS}}}h1").text = source_heading
        for paragraph in chapter.paragraphs:
            etree.SubElement(body, f"{{{XHTML_NS}}}p").text = paragraph
        chapter.paragraphs = _mark_units(body, sentence_split=config.SENTENCE_LEVEL_SPLIT)
        if chapter.title and not source_heading:
            heading = etree.Element(f"{{{XHTML_NS}}}h1")
            heading.text = chapter.title
            body.insert(0, heading)
        chapter.template = etree.tostring(body, encoding="unicode")

    return chapters
