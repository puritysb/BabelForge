"""Assemble a bilingual EPUB from a TranslatedBook.

The output strictly follows the contract in
crosspoint-agentdeck/docs/bilingual-epub.md:
  - block-level <p> only (no inline <span>)
  - token-exact class="cp-original" / class="cp-translation"
  - paragraph pairs alternating per source paragraph

Skeleton derived from crosspoint-agentdeck/scripts/generate_bilingual_test_epub.py
(EPUB 3 minimal, mimetype stored uncompressed first).
"""
from __future__ import annotations
import json
import os
import sys
import uuid
import zipfile
from datetime import datetime, timezone

import config


def _xml_escape(s: str) -> str:
    # First UNESCAPE any entities already in the text (the translator
    # sometimes echoes "&amp;" back when the source had "&"), then escape
    # exactly once. Repeat unescape until stable so cascaded entities like
    # "&amp;amp;amp;" collapse to "&" before we re-escape.
    from html import unescape
    s = s or ""
    prev = None
    while prev != s:
        prev = s
        s = unescape(s)
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


def _build_chapter_xhtml(title: str, pairs: list[tuple[str, str]],
                         source_lang: str | None = None,
                         translation_lang: str | None = None) -> str:
    src_lang = source_lang or config.SOURCE_LANG
    tr_lang = translation_lang or config.TRANSLATION_LANG
    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<!DOCTYPE html>',
        '<html xmlns="http://www.w3.org/1999/xhtml"',
        '      xmlns:epub="http://www.idpf.org/2007/ops">',
        '<head><meta charset="utf-8"/>',
        '<link rel="stylesheet" type="text/css" href="style.css"/>',
        f'<title>{_xml_escape(title)}</title></head>',
        '<body>',
    ]
    if title:
        parts.append(f'<h1>{_xml_escape(title)}</h1>')
    for original, korean in pairs:
        # Inline line-height as a belt-and-suspenders guarantee in case the
        # firmware's CssParser doesn't honor the linked stylesheet. xml:lang
        # is emitted alongside cp-* so the same EPUB works under the v28
        # hybrid parser (cp-* wins, xml:lang adds accessibility) AND under
        # any standard-conscious reader that only honors xml:lang.
        parts.append(
            f'<p class="{config.ORIGINAL_CLASS}" xml:lang="{src_lang}" '
            f'style="line-height:1.55;">{_xml_escape(original)}</p>'
        )
        if korean:  # omit empty translation paragraph entirely (no tofu block)
            parts.append(
                f'<p class="{config.TRANSLATION_CLASS}" xml:lang="{tr_lang}" '
                f'style="line-height:1.65;">{_xml_escape(korean)}</p>'
            )
    parts.append("</body></html>")
    return "".join(parts)


def _cover_meta(cover_href: str | None) -> tuple[str, str]:
    """Return (manifest_item_xml, metadata_meta_xml) for a cover, or empty."""
    if not cover_href:
        return ("", "")
    # EPUB 3: properties="cover-image" on the item. EPUB 2: meta name=cover.
    manifest = (
        f'    <item id="cover-image" href="{cover_href}" '
        f'media-type="image/jpeg" properties="cover-image"/>\n'
    )
    meta = '    <meta name="cover" content="cover-image"/>\n'
    return (manifest, meta)


def _detect_image_format(data: bytes) -> tuple[str, str]:
    """Return (extension, media_type) from magic bytes."""
    if data.startswith(b"\xff\xd8\xff"):
        return (".jpg", "image/jpeg")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return (".png", "image/png")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return (".gif", "image/gif")
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return (".webp", "image/webp")
    return (".jpg", "image/jpeg")  # assume JPEG


def _build_cover_xhtml(cover_href: str) -> str:
    """A spine entry that shows the cover image full-bleed. Without this, the
    device opens on the first chapter instead of the cover — the user sees no
    cover 'page' even though cover.jpg is in the package."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><meta charset="utf-8"/><title>Cover</title>
<style type="text/css">body{{margin:0;padding:0;background:#fff;}}img{{max-width:100%;height:auto;display:block;margin:0 auto;}}</style>
</head>
<body><div><img src="{cover_href}" alt="cover"/></div></body>
</html>"""


def _build_css() -> str:
    """Reader-friendly stylesheet. RIDIBatang is preferred for Korean; the
    device falls back to Noto Serif / Noto Sans for Latin. Line-height and
    margins give the prose breathing room (the user's "lines stuck together"
    complaint)."""
    return """/* OpenClaw book_translator — bilingual reader stylesheet */
@charset "utf-8";
body {
  font-family: "WagahaiBilingual", "RIDIBatang", "IPAexMincho", "Noto Serif", "Noto Sans", serif;
  line-height: 1.6;
  margin: 0.4em 0.6em;
  text-align: justify;
  hyphens: auto;
  -epub-hyphens: auto;
}
h1 {
  font-size: 1.25em;
  text-align: center;
  margin: 1.2em 0 0.9em 0;
  text-indent: 0;
  line-height: 1.3;
}
p {
  margin: 0 0 0.55em 0;
  text-indent: 1.1em;
  line-height: 1.6;
}
p.cp-original {
  line-height: 1.55;
  margin-bottom: 0.2em;
}
p.cp-translation {
  line-height: 1.65;
  margin-bottom: 0.9em;
  text-indent: 1.1em;
}
h1 + p, p:first-child {
  text-indent: 0;
}
"""


def _build_content_opf(book_id: str, title: str, author: str,
                       chapter_files: list[str],
                       cover_href: str | None = None,
                       language: str | None = None) -> str:
    lang = language or config.SOURCE_LANG
    manifest_items = "\n".join(
        f'    <item id="ch{i}" href="{f}" media-type="application/xhtml+xml"/>'
        for i, f in enumerate(chapter_files)
    )
    spine = "\n".join(f"    <itemref idref=\"ch{i}\"/>"
                      for i in range(len(chapter_files)))
    nav_item = (
        '    <item id="nav" href="nav.xhtml" '
        'media-type="application/xhtml+xml" properties="nav"/>'
    )
    css_item = (
        '    <item id="css" href="style.css" '
        'media-type="text/css"/>'
    )
    cover_manifest, cover_meta = _cover_meta(cover_href)
    # When we have a cover image, add a cover.xhtml page entry so the spine
    # can open on the cover (device firmware shows spine[0] as the first page;
    # without a cover page, cover-image only drives library thumbnails).
    cover_page_manifest = ""
    cover_page_spine = ""
    if cover_href:
        cover_page_manifest = (
            '    <item id="cover-page" href="cover.xhtml" '
            'media-type="application/xhtml+xml"/>\n'
        )
        cover_page_spine = '    <itemref idref="cover-page"/>\n'
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package version="3.0" xml:lang="{lang}"
         xmlns="http://www.idpf.org/2007/opf"
         unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:uuid:{book_id}</dc:identifier>
    <dc:title>{_xml_escape(title)}</dc:title>
    <dc:creator>{_xml_escape(author)}</dc:creator>
    <dc:language>{lang}</dc:language>
    <dc:date>{datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}</dc:date>
    <dc:publisher>OpenClaw book_translator</dc:publisher>
    <meta property="dcterms:modified">{datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}</meta>
{cover_meta}  </metadata>
  <manifest>
{nav_item}
{css_item}
{cover_page_manifest}{cover_manifest}{manifest_items}
  </manifest>
  <spine>
{cover_page_spine}    <itemref idref="nav"/>
{spine}
  </spine>
</package>
"""


def _build_nav_xhtml(title: str, chapter_files: list[tuple[int, str]]) -> str:
    """EPUB 3 required nav document (also serves as TOC)."""
    lis = "\n".join(
        f'      <li><a href="chapter{idx}.xhtml">{_xml_escape(t)}</a></li>'
        for idx, t in chapter_files
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:epub="http://www.idpf.org/2007/ops">
<head><meta charset="utf-8"/><title>{_xml_escape(title)}</title></head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>Contents</h1>
    <ol>
{lis}
    </ol>
  </nav>
</body>
</html>
"""


def _build_toc_ncx(book_id: str, chapter_titles: list[str]) -> str:
    navpoints = []
    for i, t in enumerate(chapter_titles, 1):
        navpoints.append(f"""    <navPoint id="navpoint-{i}" playOrder="{i}">
      <navLabel><text>{_xml_escape(t)}</text></navLabel>
      <content src="chapter{i}.xhtml"/>
    </navPoint>""")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="urn:uuid:{book_id}"/>
    <meta name="dtb:depth" content="1"/>
  </head>
  <docTitle><text>{_xml_escape(chapter_titles[0] if chapter_titles else "Book")}</text></docTitle>
  <navMap>
{chr(10).join(navpoints)}
  </navMap>
</ncx>
"""


def assemble(book: dict, out_path: str | None = None,
             cover_bytes: bytes | None = None,
             source_lang: str | None = None,
             translation_lang: str | None = None) -> str:
    """Write a bilingual EPUB.

    book: {"title", "author", "chapters": [{"title", "pairs": [(orig, ko), ...]}]}
          optional "source_lang" overrides the default (e.g. "ja" for Japanese).
    out_path: destination; defaults to data/library/<safe-title>.epub
    cover_bytes: optional JPEG/PNG image bytes for the book cover.
    source_lang / translation_lang: override book-level language tags
                 (default: config.SOURCE_LANG / config.TRANSLATION_LANG).
    """
    base_title = book["title"]
    title = f"{base_title} {config.BILINGUAL_SUFFIX}"
    author = book.get("author") or "Unknown"
    book_id = uuid.uuid4().hex
    src_lang = source_lang or book.get("source_lang") or config.SOURCE_LANG
    tr_lang = translation_lang or book.get("translation_lang") or config.TRANSLATION_LANG

    chapters = book.get("chapters") or []
    if not chapters:
        raise ValueError("assemble: no chapters to write")
    missing = []
    for ci, ch in enumerate(chapters, 1):
        for pi, pair in enumerate(ch.get("pairs") or [], 1):
            korean = pair[1] if len(pair) >= 2 else ""
            if not str(korean).strip():
                missing.append((ci, pi, ch.get("title") or "Untitled"))
    if missing:
        preview = ", ".join(
            f"ch{ci} unit{pi} {title!r}" for ci, pi, title in missing[:5]
        )
        extra = "" if len(missing) <= 5 else f", +{len(missing) - 5} more"
        raise ValueError(
            f"assemble: {len(missing)} blank translations; refusing to write "
            f"incomplete EPUB ({preview}{extra})"
        )

    if out_path is None:
        safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in base_title)[:80]
        out_path = os.path.join(config.LIBRARY_DIR, f"{safe or 'book'}.epub")
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    # Resolve cover embedding details up front so the manifest can reference it.
    cover_href: str | None = None
    if cover_bytes:
        ext, _media = _detect_image_format(cover_bytes)
        cover_href = f"cover{ext}"

    chapter_titles: list[tuple[int, str]] = []
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        # mimetype MUST be the first entry, stored uncompressed (EPUB spec).
        z.writestr("mimetype", "application/epub+zip",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0" encoding="utf-8"?>\n'
                   '<container version="1.0"'
                   ' xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf"'
                   ' media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')

        if cover_href and cover_bytes:
            z.writestr(f"OEBPS/{cover_href}", cover_bytes)
            z.writestr("OEBPS/cover.xhtml", _build_cover_xhtml(cover_href))

        chapter_files = []
        for i, ch in enumerate(chapters, 1):
            fname = f"chapter{i}.xhtml"
            z.writestr(f"OEBPS/{fname}",
                       _build_chapter_xhtml(ch["title"], ch["pairs"],
                                            source_lang=src_lang,
                                            translation_lang=tr_lang))
            chapter_files.append(fname)
            chapter_titles.append((i, ch["title"]))

        z.writestr("OEBPS/nav.xhtml", _build_nav_xhtml(title, chapter_titles))
        z.writestr("OEBPS/style.css", _build_css())
        z.writestr("OEBPS/content.opf",
                   _build_content_opf(book_id, title, author, chapter_files,
                                      cover_href=cover_href, language=src_lang))
        z.writestr("OEBPS/toc.ncx",
                   _build_toc_ncx(book_id, [t for _, t in chapter_titles]))

    return out_path


def main():
    """Read book JSON on stdin (from translate.py stdout), write EPUB, print path."""
    book = json.loads(sys.stdin.read())
    path = assemble(book)
    print(path)


if __name__ == "__main__":
    main()
