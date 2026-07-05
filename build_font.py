"""Build a book-specific merged cpfont for the Xteink reader.

The firmware loads ONE font family for a whole book and has no runtime
per-glyph fallback. So a Japanese→Korean bilingual book needs a single font
that carries BOTH scripts. This script:

  1. Computes the union of codepoints actually used in the source text AND the
     Korean translation (so the font stays small — a full pan-CJK font at
     ~17K glyphs × 4 sizes overflows ESP32-C3 RAM during the font scan).
  2. Delegates to the firmware's fontconvert_sdcard.py with a primary font for
     the source language + a fallback font for Korean, using the new
     --coverage-file subset flag. At build time fontconvert rasterizes each
     codepoint from the primary, falling back to the Korean font when the
     primary lacks it — producing one merged .cpfont per size.

Font selection by source language:
  ja/zh → primary IPAexMincho (kanji+kana), fallback RIDIBatang (hangul)
  *     → primary RIDIBatang (latin+hangul is enough for en/eu→ko)

Usage:
  # Full build (after translation is done, so KO coverage is complete):
  ./build_font.py --source-lang ja \\
      --source-text data/cache/wagahai/book.txt \\
      --translation data/checkpoints/wagahai.json \\
      --family WagahaiBilingual

  # Translation not done yet: KO coverage is empty, font has JP only
  # (rebuild after translation to add the hangul glyphs).
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import tempfile

import config

# ─── Font sources ───
FORK_FONT_SCRIPTS = os.path.expanduser(
    "~/github/crosspoint-agentdeck/lib/EpdFont/scripts")
FONTPYTHON = os.path.join(FORK_FONT_SCRIPTS, ".venv", "bin", "python3")
FONTCONVERT = os.path.join(FORK_FONT_SCRIPTS, "fontconvert_sdcard.py")
IPAEXMINCHO = os.path.join(FORK_FONT_SCRIPTS, "downloaded_fonts", "ipaexm.ttf")
RIDIBATANG = os.path.join(FORK_FONT_SCRIPTS, "downloaded_fonts", "RIDIBatang.otf")

CJK_SOURCES = {"ja", "zh"}


def select_fonts(source_lang: str) -> tuple[str, str | None, str]:
    """Return (primary_font, fallback_font, intervals_preset) for a language."""
    lang = (source_lang or "en").split("-")[0].lower()
    if lang in CJK_SOURCES:
        # IPAexMincho carries kanji+kana+latin; RIDIBatang fills hangul.
        return (IPAEXMINCHO, RIDIBATANG, "reading,cjk,hangul")
    # en/eu → RIDIBatang alone covers latin + hangul.
    return (RIDIBATANG, None, "reading,hangul")


def collect_codepoints(*paths: str) -> set[int]:
    """Union of every codepoint in the given text/JSON files.

    For .json translation checkpoints/books, walks every original+translated
    string so both source-language and Korean glyphs are counted. For .txt it
    reads raw characters. For .epub it extracts text via ebooklib.
    """
    cps: set[int] = set()
    for path in paths:
        if not path or not os.path.isfile(path):
            continue
        ext = os.path.splitext(path)[1].lower()
        if ext == ".json":
            _walk_json_strings(json.load(open(path, encoding="utf-8")), cps)
        elif ext == ".epub":
            _collect_from_epub(path, cps)
        else:  # .txt and anything else: raw text
            with open(path, encoding="utf-8") as f:
                for ch in f.read():
                    cps.add(ord(ch))
    return cps


def _walk_json_strings(obj, cps: set[int]) -> None:
    if isinstance(obj, str):
        for ch in obj:
            cps.add(ord(ch))
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk_json_strings(v, cps)
    elif isinstance(obj, list):
        for v in obj:
            _walk_json_strings(v, cps)


def _collect_from_epub(path: str, cps: set[int]) -> None:
    from ebooklib import epub
    book = epub.read_epub(path, options={"ignore_ncx": True})
    for item in book.get_items_of_type(9):  # ITEM_DOCUMENT
        html = item.get_content().decode("utf-8", errors="replace")
        from bs4 import BeautifulSoup
        for ch in BeautifulSoup(html, "lxml").get_text():
            cps.add(ord(ch))


def write_coverage_file(cps: set[int], path: str) -> int:
    """Write one hex codepoint per line. Returns count written."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {len(cps)} codepoints (book-specific subset)\n")
        for cp in sorted(cps):
            f.write(f"0x{cp:04X}\n")
    return len(cps)


def build(source_lang: str, source_text: str,
          translation: str | None, family: str,
          sizes: list[int] | None = None,
          output_dir: str | None = None) -> str:
    primary, fallback, intervals = select_fonts(source_lang)
    sizes = sizes or [12, 14, 16, 18]
    if not os.path.isfile(primary):
        raise FileNotFoundError(f"primary font missing: {primary}")
    if fallback and not os.path.isfile(fallback):
        raise FileNotFoundError(f"fallback font missing: {fallback}")
    if not os.path.isfile(FONTPYTHON):
        raise FileNotFoundError(f"fontconvert venv missing: {FONTPYTHON}")

    # 1. Compute coverage from source + translation (if available).
    cps = collect_codepoints(source_text, translation)
    # Always include the reading essentials (ASCII punctuation/digits) so the
    # reader UI, page numbers, and menus render even if the book lacks them.
    for cp in range(0x20, 0x7F):
        cps.add(cp)
    # Always include the REPLACEMENT char (fontconvert adds it anyway, but the
    # coverage filter would strip it without this).
    cps.add(0xFFFD)

    output_dir = output_dir or os.path.join(config.DATA_DIR, "fonts", family)
    os.makedirs(output_dir, exist_ok=True)
    cov_path = os.path.join(output_dir, "coverage.txt")
    n = write_coverage_file(cps, cov_path)
    print(f"[build_font] coverage: {n} codepoints → {cov_path}")

    # 2. Run fontconvert with the merged primary+fallback and subset.
    cmd = [
        FONTPYTHON, FONTCONVERT,
        "--intervals", intervals,
        "--sizes", ",".join(str(s) for s in sizes),
        "--name", family,
        "--output-dir", output_dir + "/",
        "--coverage-file", cov_path,
        "--regular", primary,
    ]
    if fallback:
        cmd += ["--fallback-regular", fallback]
    print("[build_font] running fontconvert…")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"fontconvert failed (exit {result.returncode})")

    cpfonts = sorted(
        os.path.join(output_dir, f)
        for f in os.listdir(output_dir)
        if f.endswith(".cpfont")
    )
    total = sum(os.path.getsize(f) for f in cpfonts)
    print(f"[build_font] {len(cpfonts)} files, "
          f"{total / 1024 / 1024:.2f} MB total → {output_dir}")
    for f in cpfonts:
        print(f"  {os.path.basename(f)}: "
              f"{os.path.getsize(f) / 1024:.0f} KB")
    return output_dir


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source-lang", required=True,
                   help="Source language code (ja, zh, en, …).")
    p.add_argument("--source-text", required=True,
                   help="Path to the source .txt/.epub (for source-script glyphs).")
    p.add_argument("--translation",
                   help="Path to translated book JSON or checkpoint (for KO glyphs). "
                        "Optional — omit to build a JP-only font, rebuild later.")
    p.add_argument("--family", required=True,
                   help="Output font family name (shows in device Font Family list).")
    p.add_argument("--sizes", default="12,14,16,18",
                   help="Comma-separated point sizes (default: 12,14,16,18).")
    p.add_argument("--output-dir", help="Output directory (default: data/fonts/<family>).")
    args = p.parse_args()

    out = build(
        source_lang=args.source_lang,
        source_text=args.source_text,
        translation=args.translation,
        family=args.family,
        sizes=[int(s) for s in args.sizes.split(",")],
        output_dir=args.output_dir,
    )
    print(f"\nDone. cpfont files in: {out}")


if __name__ == "__main__":
    main()
