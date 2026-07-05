"""Extract a per-chapter paragraph list from a fetched source.

Input: SourceText (path to .epub or .txt)
Output: list[Chapter(title, paragraphs=[str, ...])]

Cleaning rules:
- Strip Project Gutenberg license headers/footers (*** START OF / *** END OF)
- Drop empty paragraphs and pure-boilerplate lines
- Preserve chapter/TOC structure so assemble.py can rebuild spine
"""
from __future__ import annotations
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from bs4 import BeautifulSoup
from sources import SourceText
import config


# Sentence splitter for SENTENCE_LEVEL_SPLIT mode. Splits on . ! ? followed by
# whitespace + a capital letter / quote. Protects common abbreviations so
# "Mr. Smith" stays one sentence. Good enough for prose; not perfect for
# heavy dialogue or legal text.
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
    parts = [p for p in parts if len(p) > 2]
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
    paragraphs: list[str] = field(default_factory=list)


# Gutenberg's "*** START OF THE PROJECT GUTENBERG EBOOK X ***" / "*** END OF ..."
GUTEN_START = re.compile(r"\*\*\s*START\s+OF.*?\*\*\*", re.IGNORECASE | re.DOTALL)
GUTEN_END = re.compile(r"\*\*\s*END\s+OF.*?\*\*\*", re.IGNORECASE | re.DOTALL)
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

# Safety cap: never produce a single chapter so large the ESP32-C3 can't
# reflow it on Bilingual Toggle without an obvious hitch. Each chunk becomes a
# separate xhtml in the EPUB spine, keeping section cache size bounded.
# Units = cp-original/cp-translation PAIRS (sentence or paragraph depending
# on SENTENCE_LEVEL_SPLIT). Sentences are short, so allow more of them.
MAX_PARAGRAPHS_PER_CHAPTER = 80


def extract(src: SourceText) -> list[Chapter]:
    if src.format == "epub":
        return _extract_epub(src.path)
    if src.format == "txt":
        return _extract_txt(src.path)
    raise ValueError(f"unsupported source format: {src.format}")


# ─── EPUB ───
def _extract_epub(path: str) -> list[Chapter]:
    import ebooklib
    from ebooklib import epub
    book = epub.read_epub(path, options={"ignore_ncx": True})
    chapters: list[Chapter] = []
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        html = item.get_content().decode("utf-8", errors="replace")
        if not html.strip():
            continue
        soup = BeautifulSoup(html, "lxml")
        # Chapter title: prefer an explicit heading; else use the item name.
        title = ""
        for tag in ("h1", "h2", "h3"):
            h = soup.find(tag)
            if h and h.get_text(strip=True):
                title = h.get_text(strip=True)[:200]
                break
        if not title:
            title = (item.get_name() or "Chapter").replace(".xhtml", "").replace(".html", "")
        paragraphs: list[str] = []
        for p in soup.find_all(["p", "li"]):
            text = p.get_text(" ", strip=True)
            if text and len(text) > 1:
                paragraphs.append(text)
        if paragraphs:
            # Apply sentence-level splitting if enabled (default).
            units = _paragraphs_to_units(paragraphs)
            chapters.append(Chapter(title=title, paragraphs=units))
    if not chapters:
        raise ValueError(f"no chapters extracted from EPUB: {path}")
    return chapters


# ─── Plain text ───
def _extract_txt(path: str) -> list[Chapter]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()

    # Slice off the Gutenberg boilerplate if present.
    m = GUTEN_START.search(raw)
    if m:
        raw = raw[m.end():]
    m = GUTEN_END.search(raw)
    if m:
        raw = raw[:m.start()]
    # Strip the duplicated "Produced by ... pgdp.net" credits block that often
    # immediately follows the START marker.
    raw = GUTEN_PRODUCED.sub("", raw, count=1)

    # Normalize whitespace into paragraphs (blank-line separated).
    # Collapse runs of whitespace inside a paragraph.
    paras: list[str] = []
    for block in re.split(r"\n\s*\n", raw):
        text = re.sub(r"\s+", " ", block).strip()
        if len(text) > 1:
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
    for p in paras:
        first_line = p.split("\n")[0].strip()
        is_heading = (
            bool(CHAPTER_RE.match(p))
            or (len(p) <= 80 and len(p.split()) <= 6 and bool(CHAPTER_RE.match(p)))
        )
        if is_heading:
            current = Chapter(title=p.strip()[:200])
            chapters.append(current)
        else:
            current.paragraphs.append(p)

    # Apply sentence-level splitting to each chapter's raw paragraphs.
    for ch in chapters:
        ch.paragraphs = _paragraphs_to_units(ch.paragraphs)

    # Drop empty chapters (e.g. trailing Preface with no body).
    chapters = [c for c in chapters if c.paragraphs]
    if not chapters:
        chapters = [Chapter(title="Untitled", paragraphs=_paragraphs_to_units(paras))]

    # If the first chapter is short it's almost certainly front-matter
    # boilerplate — a "Translated by …" credit line, a publication-city block,
    # a copyright line, or a duplicated Gutenberg header. Merge it into the
    # next chapter so the TOC opens on a substantive section and the reader
    # doesn't see a near-empty page right after the cover. Threshold is
    # generous (≤8 paragraphs) because credit blocks sometimes span a few
    # short lines. Also forces merge when the title matches a credit pattern
    # regardless of size.
    CREDIT_TITLE_RE = re.compile(
        r"^(TRANSLATED\s+BY|COPYRIGHT|PUBLISHED|BY\s+[A-Z]|DEDICATION|"
        r"ABOUT\s+THE|COLOPHON|PRODUCED\s+BY)",
        re.IGNORECASE,
    )
    while len(chapters) >= 2:
        first = chapters[0]
        is_credit_title = bool(CREDIT_TITLE_RE.match(first.title or ""))
        is_short = len(first.paragraphs) <= 8
        if not (is_credit_title or is_short):
            break
        # Merge first into second; keep the real section's title.
        chapters[1].paragraphs = first.paragraphs + chapters[1].paragraphs
        chapters.pop(0)
        # Only merge once-or-twice — stop if the new first chapter looks
        # like real content.
        if not (is_credit_title or is_short):
            break

    # Safety net: split any chapter that's still too big for the device to
    # reflow quickly. This catches aphorism books / loose-prose sources where
    # the section detector finds nothing.
    chapters = _split_oversized(chapters)
    return chapters


def _split_oversized(chapters: list[Chapter],
                     cap: int = MAX_PARAGRAPHS_PER_CHAPTER) -> list[Chapter]:
    out: list[Chapter] = []
    for i, ch in enumerate(chapters, 1):
        if len(ch.paragraphs) <= cap:
            out.append(ch)
            continue
        # Split into cap-sized sub-chapters with a clear (Part N) suffix so the
        # TOC still reads naturally and the device can paginate them fast.
        n = len(ch.paragraphs)
        chunks = [ch.paragraphs[i:i+cap] for i in range(0, n, cap)]
        total = len(chunks)
        for j, chunk in enumerate(chunks, 1):
            title = ch.title if total == 1 else f"{ch.title} (Part {j}/{total})"
            out.append(Chapter(title=title, paragraphs=chunk))
    return out
