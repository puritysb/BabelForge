"""Deliver a book + its font to the Xteink device in one shot.

Run this when the device is in File Transfer mode. It:
  1. Pushes the book-specific merged cpfont family (so JP+KO both render).
  2. Pushes the EPUB directly (and/or confirms the watcher queued it).
  3. Sends a LINE notification on success.

Usage:
  deliver.py wagahai            # deliver by req_id
  deliver.py --family WagahaiBilingual --epub "data/library/吾輩は猫である.epub"
"""
from __future__ import annotations
import argparse
import json
import os
import sys

import config
import catalog
import pending_queue
import search as search_mod
from device_push import push_font_family, push_to_device


def deliver(req_id: str | None = None, family: str | None = None,
            epub: str | None = None) -> None:
    # Resolve from req_id if given
    if req_id:
        entry = catalog.get(req_id)
        if not entry:
            print(f"unknown req_id: {req_id}", file=sys.stderr)
            sys.exit(2)
        epub = epub or entry.get("epub_path")
        # Derive family name from a convention: <Title>Bilingual, or look in
        # data/fonts/ for the first family dir.
    if not family:
        fonts_root = os.path.join(config.DATA_DIR, "fonts")
        if os.path.isdir(fonts_root):
            fams = sorted(os.listdir(fonts_root))
            if fams:
                family = fams[0]
    font_dir = (os.path.join(config.DATA_DIR, "fonts", family)
                if family else None)

    results = {"font": None, "epub": None}

    # 1. Font
    if family and font_dir and os.path.isdir(font_dir):
        print(f"Pushing font family '{family}'…", file=sys.stderr)
        r = push_font_family(family, font_dir)
        results["font"] = r.to_dict()
        if r.pushed:
            print(f"  ✓ font pushed ({r.elapsed_ms}ms)", file=sys.stderr)
        else:
            print(f"  ✗ font: {r.error}", file=sys.stderr)
    else:
        print("no font to push (skipping)", file=sys.stderr)

    # 2. EPUB
    if epub and os.path.isfile(epub):
        print(f"Pushing EPUB {os.path.basename(epub)}…", file=sys.stderr)
        r = push_to_device(epub)
        results["epub"] = r.to_dict()
        if r.pushed:
            print(f"  ✓ epub pushed ({r.elapsed_ms}ms)", file=sys.stderr)
            # Remove from pending queue if it was there.
            pending_queue.remove(epub)
        elif r.skipped:
            print(f"  ⏳ epub deferred: {r.error}", file=sys.stderr)
        else:
            print(f"  ✗ epub: {r.error}", file=sys.stderr)
    else:
        print("no epub to push", file=sys.stderr)

    # 3. Notify
    font_ok = results["font"] and results["font"].get("pushed")
    epub_ok = results["epub"] and results["epub"].get("pushed")
    if font_ok or epub_ok:
        parts = []
        if font_ok:
            parts.append(f"폰트 {family}")
        if epub_ok:
            parts.append("EPUB")
        search_mod.notify_line(
            f"✅ 기기 전송 완료: {', '.join(parts)}\n"
            f"Settings → Reader → Font Family → {family or 'RIDIBatang'} 선택 후 "
            f"책을 열면 양국어(일본어/한국어)가 모두 렌더링됩니다."
        )

    print(json.dumps(results, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("req_id", nargs="?", help="Catalog request id (e.g. wagahai).")
    ap.add_argument("--family", help="cpfont family name to push.")
    ap.add_argument("--epub", help="EPUB path to push.")
    args = ap.parse_args()
    deliver(req_id=args.req_id, family=args.family, epub=args.epub)


if __name__ == "__main__":
    main()
