"""Assemble portable, structured bilingual EPUB 3 publications.

The output is readable in ordinary EPUB readers. Language markup, semantic
XHTML and the standard navigation document do not require custom firmware.
"""
from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import os
from pathlib import PurePosixPath
import re
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone

from lxml import etree

import config
from epub_markup import (EPUB_NS, XHTML_NS, XML_NS, plain_text, sanitize_inline)


def _xml_escape(value: str) -> str:
    return html.escape(str(value or ""), quote=True)


def _language(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", value):
        raise ValueError(f"invalid language tag: {value!r}")
    return value


def _inline_node(text: str, kind: str, language: str, *, strip_ids=False):
    node = etree.fromstring(
        f'<span xmlns="{XHTML_NS}" xmlns:epub="{EPUB_NS}">'
        f'{sanitize_inline(text)}</span>'.encode(),
        etree.XMLParser(resolve_entities=False, no_network=True))
    node.set("class", kind)
    node.set("lang", language)
    node.set(f"{{{XML_NS}}}lang", language)
    if strip_ids:
        for child in node.iter():
            child.attrib.pop("id", None)
            if child is not node:
                child.attrib.pop("lang", None)
                child.attrib.pop(f"{{{XML_NS}}}lang", None)
    return node


def _build_chapter_xhtml(title: str, pairs: list[tuple[str, str]],
                         source_lang: str | None = None,
                         translation_lang: str | None = None,
                         template: str | None = None) -> str:
    src_lang = _language(source_lang or config.SOURCE_LANG)
    tr_lang = _language(translation_lang or config.TRANSLATION_LANG)
    root = etree.Element(f"{{{XHTML_NS}}}html",
                         nsmap={None: XHTML_NS, "epub": EPUB_NS})
    root.set("lang", src_lang)
    root.set(f"{{{XML_NS}}}lang", src_lang)
    head = etree.SubElement(root, f"{{{XHTML_NS}}}head")
    etree.SubElement(head, f"{{{XHTML_NS}}}meta", charset="utf-8")
    etree.SubElement(head, f"{{{XHTML_NS}}}title").text = title or "Chapter"
    etree.SubElement(head, f"{{{XHTML_NS}}}link", rel="stylesheet",
                     type="text/css", href="style.css")
    if template:
        body = etree.fromstring(template.encode(),
                               etree.XMLParser(resolve_entities=False, no_network=True))
        if etree.QName(body).localname != "body":
            raise ValueError("chapter template must be an XHTML body")
        for node in body.iter():
            if isinstance(node.tag, str) and not node.tag.startswith("{"):
                node.tag = f"{{{XHTML_NS}}}{node.tag}"
        units = body.xpath('//*[@data-bf-unit]')
        indices = [node.get("data-bf-unit") for node in units]
        if indices != [str(i) for i in range(len(pairs))]:
            raise ValueError("chapter template/pair alignment mismatch")
        for node, (original, translation) in zip(units, pairs):
            # The template must be bound to the exact units sent to translation.
            existing = html.escape(node.text or "") + "".join(
                etree.tostring(child, encoding="unicode") for child in node)
            if plain_text(existing) != plain_text(original):
                raise ValueError("chapter template source differs from translated source")
            for child in list(node):
                node.remove(child)
            node.text = None
            del node.attrib["data-bf-unit"]
            node.append(_inline_node(original, config.ORIGINAL_CLASS, src_lang))
            node.append(_inline_node(translation, config.TRANSLATION_CLASS,
                                     tr_lang, strip_ids=True))
    else:
        body = etree.Element(f"{{{XHTML_NS}}}body")
        if title:
            etree.SubElement(body, f"{{{XHTML_NS}}}h1").text = title
        for original, translation in pairs:
            paragraph = etree.SubElement(body, f"{{{XHTML_NS}}}p", {"class": "bilingual-pair"})
            paragraph.append(_inline_node(original, config.ORIGINAL_CLASS, src_lang))
            paragraph.append(_inline_node(translation, config.TRANSLATION_CLASS,
                                          tr_lang, strip_ids=True))
    root.append(body)
    return etree.tostring(root, encoding="utf-8", xml_declaration=True,
                          doctype="<!DOCTYPE html>").decode()


def _detect_image_format(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif", "image/gif"
    raise ValueError("cover must be a JPEG, PNG or GIF image")


def _cover_meta(cover_href: str | None, media_type: str | None = None) -> tuple[str, str]:
    if not cover_href:
        return "", ""
    media_type = media_type or mimetypes.guess_type(cover_href)[0]
    if media_type not in {"image/jpeg", "image/png", "image/gif", "image/svg+xml"}:
        raise ValueError("unsupported cover media type")
    return (
        f'<item id="cover-image" href="{_xml_escape(cover_href)}" '
        f'media-type="{media_type}" properties="cover-image"/>',
        '<meta name="cover" content="cover-image"/>',
    )


def _build_cover_xhtml(cover_href: str) -> str:
    return f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="{XHTML_NS}" lang="en" xml:lang="en">
<head><meta charset="utf-8"/><title>Cover</title>
<link rel="stylesheet" type="text/css" href="style.css"/></head>
<body><div class="cover"><img src="{_xml_escape(cover_href)}" alt="Book cover"/></div></body>
</html>'''


def _build_css() -> str:
    return f'''@charset "utf-8";
body {{ font-family: serif; line-height: 1.6; margin: 1em; }}
h1, h2, h3 {{ line-height: 1.3; break-after: avoid; }}
p {{ margin: 0 0 1em; }}
.{config.ORIGINAL_CLASS}, .{config.TRANSLATION_CLASS} {{ display: block; }}
.{config.ORIGINAL_CLASS} {{ margin-bottom: 0.3em; }}
.{config.ORIGINAL_CLASS}[lang="grc"] {{ font-family: Georgia, "Times New Roman", serif; }}
.{config.TRANSLATION_CLASS} {{ margin-bottom: 0.8em; }}
code {{ overflow-wrap: anywhere; word-wrap: break-word; }}
img {{ max-width: 100%; height: auto; }}
.cover {{ text-align: center; }}
blockquote {{ margin: 1em 1.5em; }}
pre, .verse, .poem {{ white-space: pre-wrap; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ padding: 0.4em; border: 1px solid; vertical-align: top; }}
a {{ text-decoration: underline; }}
'''


def _build_content_opf(book_id: str, title: str, author: str,
                       chapter_files: list[str], cover_href: str | None = None,
                       language: str | None = None, translation_lang: str | None = None,
                       assets: dict[str, bytes] | None = None,
                       provenance: dict | None = None,
                       chapter_properties: dict[str, list[str]] | None = None) -> str:
    lang = _language(language or config.SOURCE_LANG)
    tr_lang = _language(translation_lang or config.TRANSLATION_LANG)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
    ]
    spine = []
    cover_manifest, cover_metadata = _cover_meta(cover_href)
    if cover_href:
        manifest += [cover_manifest, '<item id="cover-page" href="cover.xhtml" media-type="application/xhtml+xml"/>']
        spine.append('<itemref idref="cover-page"/>')
    for i, name in enumerate(chapter_files):
        props = " ".join((chapter_properties or {}).get(name, []))
        attributes = f' properties="{props}"' if props else ""
        manifest.append(f'<item id="ch{i}" href="{_xml_escape(name)}" media-type="application/xhtml+xml"{attributes}/>')
        spine.append(f'<itemref idref="ch{i}"/>')
    for i, name in enumerate(sorted(assets or {})):
        media = mimetypes.guess_type(name)[0]
        if not media:
            raise ValueError(f"unknown asset media type: {name}")
        manifest.append(f'<item id="asset{i}" href="{_xml_escape(name)}" media-type="{media}"/>')
    extra = ""
    provenance = provenance or {}
    if provenance.get("url"):
        extra += f'<dc:source>{_xml_escape(provenance["url"])}</dc:source>'
    if provenance.get("rights"):
        extra += f'<dc:rights>{_xml_escape(provenance["rights"])}</dc:rights>'
    for key in ("source", "source_id", "source_sha256", "quality_profile"):
        if provenance.get(key):
            extra += f'<meta property="bf:{key}">{_xml_escape(provenance[key])}</meta>'
    languages = "".join(f'<dc:language>{value}</dc:language>' for value in dict.fromkeys((lang, tr_lang)))
    return f'''<?xml version="1.0" encoding="utf-8"?>
<package version="3.0" xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid"
 xml:lang="{lang}" prefix="bf: https://books.foundby.kr/ns/babelforge#">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="bookid">urn:uuid:{book_id}</dc:identifier>
<dc:title>{_xml_escape(title)}</dc:title><dc:creator>{_xml_escape(author)}</dc:creator>
{languages}<dc:publisher>BabelForge</dc:publisher><dc:date>{now[:10]}</dc:date>
<meta property="dcterms:modified">{now}</meta>{cover_metadata}{extra}
</metadata><manifest>{''.join(manifest)}</manifest>
<spine toc="ncx">{''.join(spine)}</spine></package>'''


def _build_nav_xhtml(title: str, chapter_files: list[tuple[int, str]],
                     chapter_titles_ko: list[str] | None = None,
                     source_lang: str | None = None, translation_lang: str | None = None) -> str:
    """EPUB 3 required nav document (also serves as TOC).

    When chapter_titles_ko is provided, each entry shows the original title
    with the Korean translation in parentheses for bilingual navigation.
    """
    src_lang = _language(source_lang or config.SOURCE_LANG)
    tr_lang = _language(translation_lang or config.TRANSLATION_LANG)
    lis = []
    for i, (idx, t) in enumerate(chapter_files):
        ko = chapter_titles_ko[i] if chapter_titles_ko else None
        if ko:
            lis.append(
                f'      <li><a href="chapter{idx}.xhtml">'
                f'<span lang="{src_lang}" xml:lang="{src_lang}">{_xml_escape(t)}</span> '
                f'(<span lang="{tr_lang}" xml:lang="{tr_lang}">{_xml_escape(ko)}</span>)</a></li>'
            )
        else:
            lis.append(
                f'      <li><a href="chapter{idx}.xhtml">'
                f'<span lang="{src_lang}" xml:lang="{src_lang}">{_xml_escape(t)}</span></a></li>'
            )
    lis_text = "\n".join(lis)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:epub="http://www.idpf.org/2007/ops" lang="{src_lang}" xml:lang="{src_lang}">
<head><meta charset="utf-8"/><title>{_xml_escape(title)}</title></head>
<body>
  <nav epub:type="toc" id="toc">
    <h1 lang="en" xml:lang="en">Contents</h1>
    <ol>
{lis_text}
    </ol>
  </nav>
</body>
</html>
"""


def _build_toc_ncx(book_id: str, chapter_titles: list[str],
                   chapter_titles_ko: list[str] | None = None,
                   book_title: str | None = None) -> str:
    navpoints = []
    for i, t in enumerate(chapter_titles, 1):
        ko = chapter_titles_ko[i - 1] if chapter_titles_ko else None
        label = f"{_xml_escape(t)} ({_xml_escape(ko)})" if ko else _xml_escape(t)
        navpoints.append(f"""    <navPoint id="navpoint-{i}" playOrder="{i}">
      <navLabel><text>{label}</text></navLabel>
      <content src="chapter{i}.xhtml"/>
    </navPoint>""")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="urn:uuid:{book_id}"/>
    <meta name="dtb:depth" content="1"/>
  </head>
  <docTitle><text>{_xml_escape(book_title or (chapter_titles[0] if chapter_titles else "Book"))}</text></docTitle>
  <navMap>
{chr(10).join(navpoints)}
  </navMap>
</ncx>
"""


def assemble(book: dict, out_path: str | None = None,
             cover_bytes: bytes | None = None, source_lang: str | None = None,
             translation_lang: str | None = None,
             validation_report: dict | None = None) -> str:
    """Validate and atomically write a portable bilingual EPUB.

    Chapter templates preserve the source structure; plain-text input uses
    paragraph pairs. Optional chapter_titles_ko retains bilingual TOC support.
    """
    base_title = book["title"]
    title = f"{base_title} {config.BILINGUAL_SUFFIX}"
    author = book.get("author") or "Unknown"
    src_lang = _language(source_lang or book.get("source_lang") or config.SOURCE_LANG)
    tr_lang = _language(translation_lang or config.TRANSLATION_LANG)
    provenance = book.get("provenance") or {}
    identity = json.dumps([base_title, author, src_lang, tr_lang, provenance], sort_keys=True)
    book_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
    chapters = book.get("chapters") or []
    pairs = [pair for chapter in chapters for pair in chapter.get("pairs", [])]
    if not chapters or not pairs:
        raise ValueError("assemble: no translated content to write")
    if any(len(pair) != 2 or not plain_text(pair[0]) or not plain_text(pair[1]) for pair in pairs):
        raise ValueError("assemble: blank source/translation or malformed pair")
    if out_path is None:
        safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in base_title)[:70]
        digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
        out_path = os.path.join(config.LIBRARY_DIR, f"{safe or 'book'}-{digest}.epub")
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    assets: dict[str, bytes] = {}
    for chapter in chapters:
        for name, content in (chapter.get("assets") or {}).items():
            path = PurePosixPath(name)
            if (not name.startswith("assets/") or path.is_absolute()
                    or ".." in path.parts or "\\" in name or not isinstance(content, bytes)):
                raise ValueError(f"unsafe chapter asset: {name}")
            if name in assets and assets[name] != content:
                raise ValueError(f"conflicting asset content: {name}")
            assets[name] = content
    cover_href = None
    if cover_bytes:
        ext, _ = _detect_image_format(cover_bytes)
        cover_href = f"cover{ext}"
    fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(out_path), suffix=".epub")
    os.close(fd)
    try:
        with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            z.writestr("META-INF/container.xml", '<?xml version="1.0" encoding="utf-8"?>'
                       '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                       '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                       'media-type="application/oebps-package+xml"/></rootfiles></container>')
            if cover_href:
                z.writestr(f"OEBPS/{cover_href}", cover_bytes)
                z.writestr("OEBPS/cover.xhtml", _build_cover_xhtml(cover_href))
            chapter_files = []
            chapter_properties = {}
            for i, chapter in enumerate(chapters, 1):
                name = f"chapter{i}.xhtml"
                chapter_files.append(name)
                document = _build_chapter_xhtml(
                    chapter["title"], chapter["pairs"], src_lang, tr_lang,
                    template=chapter.get("template"))
                tree = etree.fromstring(document.encode())
                features = []
                for feature, namespace in (("svg", "http://www.w3.org/2000/svg"),
                                           ("mathml", "http://www.w3.org/1998/Math/MathML")):
                    if any(isinstance(node.tag, str) and node.tag.startswith("{" + namespace + "}")
                           for node in tree.iter()):
                        features.append(feature)
                chapter_properties[name] = features
                z.writestr(f"OEBPS/{name}", document)
            for name, content in assets.items():
                z.writestr(f"OEBPS/{name}", content)
            titles = [chapter["title"] for chapter in chapters]
            ko_titles = book.get("chapter_titles_ko")
            if ko_titles is not None and len(ko_titles) != len(titles):
                raise ValueError("chapter_titles_ko must align with chapters")
            z.writestr("OEBPS/nav.xhtml", _build_nav_xhtml(title, list(enumerate(titles, 1)), ko_titles, src_lang, tr_lang))
            z.writestr("OEBPS/toc.ncx", _build_toc_ncx(book_id, titles, ko_titles, book_title=title))
            z.writestr("OEBPS/style.css", _build_css())
            z.writestr("OEBPS/content.opf", _build_content_opf(
                book_id, title, author, chapter_files, cover_href, src_lang,
                tr_lang, assets, provenance, chapter_properties))
        from epub_validate import validate_epub
        report = validate_epub(temp_path, expected_pairs=len(pairs), expected_content=pairs)
        if validation_report is not None:
            validation_report.update(report)
        os.replace(temp_path, out_path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return out_path


def main():
    print(assemble(json.loads(sys.stdin.read())))


if __name__ == "__main__":
    main()
