"""Resume source-conserving editorial review of existing bilingual EPUBs.

A JSON manifest explicitly binds each existing edition to its retained source.
No publication, notifications, device delivery or source-edition substitution.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import os
import posixpath
import re
import tempfile
import threading
import unicodedata
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

import assemble
import config
import precision
import translate
from epub_markup import XHTML_NS, plain_text, sanitize_inline
from epub_validate import validate_epub
from extract import Chapter, _bounded_text_chunks, extract
from quality_audit import (SEMANTIC_AUDIT_VERSION, audit_book_quality,
                           find_translation_integrity_violations,
                           _semantic_rows, _semantic_signature)
from sources import SourceText
from state_store import file_lock

BASE = Path(__file__).resolve().parent
VERSION = "library-editorial-v1"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def path_of(value):
    path = Path(value)
    return path if path.is_absolute() else BASE / path


def normalized(value):
    # Only whitespace/canonical Unicode differ across legacy TXT serialization.
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", value))


def inner(node):
    return html.escape(node.text or "") + "".join(
        etree.tostring(c, encoding="unicode") for c in node)


def read_bilingual(path: Path):
    """Read spine order and retain source body structures, resources and cover."""
    with zipfile.ZipFile(path) as archive:
        files = {n: archive.read(n) for n in archive.namelist()}
    container = etree.fromstring(files["META-INF/container.xml"])
    opfn = container.xpath('//*[local-name()="rootfile"]/@full-path')[0]
    opf = etree.fromstring(files[opfn])
    items = {n.get("id"): n for n in opf.xpath('//*[local-name()="manifest"]/*')}
    book = translate.TranslatedBook(
        opf.xpath('//*[local-name()="title"]/text()')[0],
        (opf.xpath('//*[local-name()="creator"]/text()') or ["Unknown"])[0])
    pairs = []
    for ref in opf.xpath('//*[local-name()="spine"]/*'):
        item = items[ref.get("idref")]
        if "nav" in item.get("properties", "").split():
            continue
        name = posixpath.normpath(posixpath.join(posixpath.dirname(opfn), item.get("href")))
        if not name.endswith((".xhtml", ".html")):
            continue
        tree = etree.fromstring(files[name])
        body = tree.find(f"{{{XHTML_NS}}}body")
        source_nodes = [n for n in body.iter()
                        if set(n.get("class", "").split()) & {"cp-original", "bf-original"}]
        if not source_nodes:
            # Preserve textual edition/rights notices, not duplicate cover/nav.
            if not plain_text(etree.tostring(body, encoding="unicode")):
                continue
            if body.xpath('.//*[local-name()="nav"]'):
                continue
        titles = tree.xpath('//*[local-name()="title"]/text()')
        chapter = translate.TranslatedChapter(titles[0] if titles else Path(name).stem)
        for i, node in enumerate(source_nodes):
            target = node.getnext()
            if target is None or not (set(target.get("class", "").split()) & {"cp-translation", "bf-translation"}):
                raise ValueError(f"unpaired legacy source: {name}/{i}")
            source, korean = inner(node), inner(target)
            chapter.pairs.append((source, korean))
            pairs.append((source, korean))
            node.getparent().remove(target)
            node.set("data-bf-unit", str(i))
            node.attrib.pop("class", None)
        # Old archive links must be rehomed when preserving arbitrary assets.
        # TXT reconstruction uses only the pair corpus. Structured editions
        # without embedded resources are safe; fail rather than drop media.
        for node in body.iter():
            for key in ("href", "src"):
                value = node.get(key)
                if value and not (value.startswith(("https://", "http://", "mailto:", "#"))):
                    raise ValueError(f"structured review needs resource relocation: {name}/{value}")
        chapter.template = etree.tostring(body, encoding="unicode")
        book.chapters.append(chapter)
    if not pairs:
        raise ValueError(f"no bilingual content: {path}")
    return book


def rebase_txt(old_book, source_path: Path):
    """Restore the complete retained TXT, preserving exactly aligned old Korean.

    Legacy sentence boundaries survive inside real source paragraphs. Missing
    sentences, labels and headings become blank draft slots, never disappear.
    A differing edition is rejected rather than aligned approximately.
    """
    chapters = extract(SourceText(None, "txt", str(source_path)))
    groups = []
    for ci, chapter in enumerate(chapters):
        body = etree.fromstring(chapter.template.encode())
        parents = []
        for node in body.xpath('//*[@data-bf-unit]'):
            parent = node
            blocks = {"p", "h1", "h2", "h3", "h4", "pre", "li", "td", "th", "dt", "dd", "blockquote"}
            while etree.QName(parent).localname not in blocks and parent.getparent() is not None:
                parent = parent.getparent()
            if parent not in parents:
                parents.append(parent)
        for parent in parents:
            raw = "".join(parent.itertext())
            groups.append({"chapter": ci, "parent": parent, "raw": raw})
        chapter._review_body = body
    stream = "".join(normalized(g["raw"]) for g in groups)
    matches, cursor = [], 0
    for ci, chapter in enumerate(old_book.chapters):
        for pi, (source, korean) in enumerate(chapter.pairs):
            value = normalized(plain_text(source))
            start = stream.find(value, cursor)
            if start < 0:
                raise ValueError(f"source edition/alignment mismatch at legacy {ci}/{pi}: {value[:80]}")
            end = start + len(value)
            matches.append((start, end, korean, (ci, pi)))
            cursor = end
    result = translate.TranslatedBook(old_book.title, old_book.author)
    for ch in chapters:
        result.chapters.append(translate.TranslatedChapter(ch.title))
    offset = pointer = reused = recovered = crossings = 0
    for group in groups:
        raw, parent = group["raw"], group["parent"]
        # NFC first ensures offsets agree even with decomposed source input.
        raw = unicodedata.normalize("NFC", raw)
        positions = [i for i, c in enumerate(raw) if not c.isspace()]
        end = offset + len(positions)
        while pointer < len(matches) and matches[pointer][1] <= offset:
            pointer += 1
        local, j = [], pointer
        while j < len(matches) and matches[j][0] < end:
            start, stop, korean, key = matches[j]
            if offset <= start and stop <= end:
                local.append((start - offset, stop - offset, korean))
            else:
                crossings += 1
            j += 1
        cuts = {0, len(positions)}
        for start, stop, _ in local:
            cuts.update((start, stop))
        exact = {(a, b): ko for a, b, ko in local}
        for child in list(parent):
            parent.remove(child)
        parent.text = None
        parent.attrib.pop("data-bf-unit", None)
        chapter = result.chapters[group["chapter"]]
        ordered = sorted(cuts)
        for a, b in zip(ordered, ordered[1:]):
            left = 0 if a == 0 else positions[a]
            right = len(raw) if b == len(positions) else positions[b]
            segment = raw[left:right]
            if not segment.strip():
                continue
            korean = exact.get((a, b), "")
            bounded = _bounded_text_chunks(segment, config.TRANSLATE_BATCH_MAX_CHARS, False)
            if len(bounded) != 1:
                korean = ""  # never slice Korean by source offsets
            for part in bounded:
                if not part.strip():
                    continue
                node = etree.SubElement(parent, f"{{{XHTML_NS}}}span")
                node.text = part
                node.set("data-bf-unit", str(len(chapter.pairs)))
                chapter.pairs.append((html.escape(part), korean))
                reused += bool(korean)
                recovered += not bool(korean)
        offset = end
    for ci, chapter in enumerate(chapters):
        result.chapters[ci].template = etree.tostring(chapter._review_body, encoding="unicode")
    rebuilt = "".join(normalized(plain_text(src)) for ch in result.chapters for src, _ in ch.pairs)
    if rebuilt != stream:
        raise ValueError("reconstructed TXT source coverage differs from retained edition")
    return result, {"source_coverage_verified": True, "source_characters": len(stream),
                    "source_text_sha256": digest(stream.encode()),
                    "legacy_pairs": len(matches), "reused_pairs": reused,
                    "restored_or_resegmented_pairs": recovered,
                    "crossing_units_retranslated": crossings}


def serialize(book):
    return {"title": book.title, "author": book.author,
            **({"source_evidence": book.source_evidence} if getattr(book, "source_evidence", None) else {}),
            "chapters": [{"title": c.title, "pairs": c.pairs, "template": c.template}
                         for c in book.chapters]}


def deserialize(value):
    book = translate.TranslatedBook(value["title"], value["author"], [
        translate.TranslatedChapter(c["title"], [tuple(p) for p in c["pairs"]], c.get("template"))
        for c in value["chapters"]])
    book.source_evidence = value.get("source_evidence", {})
    return book


def load_review_sheet(directory):
    sheet = json.loads((Path(directory) / "concepts.json").read_text())
    if sheet.get("profile_hash") != precision.precision_profile_hash(sheet):
        raise ValueError("review concept profile changed without a matching hash")
    return sheet


def checked_baseline(directory, state):
    data = (Path(directory) / "baseline.json").read_bytes()
    if state.get("baseline_hash") != digest(data):
        raise ValueError("review checkpoint baseline changed")
    baseline = json.loads(data)
    if ([s for c in baseline["chapters"] for s, _ in c["pairs"]] !=
            [s for c in state["book"]["chapters"] for s, _ in c["pairs"]]):
        raise ValueError("review checkpoint changed original source text")
    return baseline


def prepare(manifest_path: Path):
    manifest = json.loads(manifest_path.read_text())
    root = path_of(manifest["review_dir"])
    for entry in manifest["editions"]:
        directory = root / entry["id"]
        directory.mkdir(parents=True, exist_ok=True)
        baseline_path = directory / "baseline.json"
        inputs = [path_of(n) for n in entry["inputs"]]
        hashes = {(str(p.relative_to(BASE)) if p.is_relative_to(BASE) else str(p)):
                  digest(p.read_bytes()) for p in inputs}
        if baseline_path.exists():
            saved = json.loads(baseline_path.read_text())
            if saved["input_hashes"] != hashes:
                raise ValueError("input EPUB changed since review preparation")
            source_bytes = path_of(entry["source_path"]).read_bytes()
            if digest(source_bytes) != saved["source_sha256"]:
                raise ValueError("retained source changed since review preparation")
            source_backup = directory / ("source-original." + entry["source_format"])
            if not source_backup.exists():
                source_backup.write_bytes(source_bytes)
            continue
        imported = read_bilingual(inputs[0])
        for alias in inputs[1:]:
            other = read_bilingual(alias)
            if ([normalized(plain_text(s)) for c in imported.chapters for s, _ in c.pairs]
                    != [normalized(plain_text(s)) for c in other.chapters for s, _ in c.pairs]):
                raise ValueError("aliases have different source editions")
        source_path = path_of(entry["source_path"])
        if entry["source_format"] == "txt":
            book, coverage = rebase_txt(imported, source_path)
        else:
            book = imported
            coverage = {"source_coverage_verified": True, "method": "pinned EPUB pair source",
                        "legacy_pairs": sum(len(c.pairs) for c in book.chapters),
                        "reused_pairs": sum(len(c.pairs) for c in book.chapters),
                        "restored_or_resegmented_pairs": 0}
            source = extract(SourceText(None, "epub", str(source_path)))
            if [normalized(plain_text(s)) for c in book.chapters for s, _ in c.pairs] != [
                    normalized(plain_text(s)) for c in source for s in c.paragraphs]:
                raise ValueError("pinned EPUB differs from bilingual source")
        book.title = entry["title"]
        book.author = entry["author"]
        # Keep hashes and the aligned editorial baseline, not old EPUB backups.
        # Live input files remain available until verified publication replaces
        # them; deletion of known duplicates belongs to that later transaction.
        (directory / ("source-original." + entry["source_format"])).write_bytes(source_path.read_bytes())
        save(baseline_path, {**serialize(book), "input_hashes": hashes,
                            "source_sha256": digest(source_path.read_bytes()),
                            "coverage": coverage, "entry": entry})
        print(json.dumps({"prepared": entry["id"], **coverage,
                          "pairs": sum(len(c.pairs) for c in book.chapters)}, ensure_ascii=False), flush=True)
    return manifest, root


def review_one(entry, root):
    directory = root / entry["id"]
    with file_lock(str(directory / "worker.lock"), blocking=False):
        (directory / "error.json").unlink(missing_ok=True)
        baseline = json.loads((directory / "baseline.json").read_text())
        state_path = directory / "checkpoint.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if state and state.get("baseline_hash") != digest((directory / "baseline.json").read_bytes()):
            raise ValueError("review checkpoint baseline changed")
        book = deserialize(state.get("book") or baseline)
        book.audit_log = state.get("audit_log", [])
        verified = set(state.get("verified", []))
        call_numbers = [int(p.stem) for p in (directory / "calls").glob("*.json") if p.stem.isdigit()]
        calls = max([state.get("calls", 0)] + call_numbers)
        profile_path = directory / "concepts.json"
        def chat(messages):
            nonlocal calls
            calls += 1
            response = translate._chat_json(messages)
            save(directory / "calls" / f"{calls:06}.json", {
                "messages": messages, "response": response,
                "utc": datetime.now(timezone.utc).isoformat()})
            return response
        def checkpoint(stage="reviewing"):
            save(state_path, {"version": VERSION, "baseline_hash": digest((directory / "baseline.json").read_bytes()),
                              "stage": stage, "book": serialize(book), "audit_log": book.audit_log,
                              "verified": sorted(verified), "calls": calls,
                              "updated_utc": datetime.now(timezone.utc).isoformat()})
        if profile_path.exists():
            sheet = load_review_sheet(directory)
        else:
            print(f"[{entry['id']}] researching concepts", flush=True)
            from mcp_client import web_search
            chapters = [Chapter(c.title, [s for s, _ in c.pairs]) for c in book.chapters]
            sheet = precision.build_concept_sheet(chapters, book.title, book.author,
                entry["language"], entry["source_research"], chat, web_search)
            save(profile_path, sheet)
        # Draft only recovered source, with the production translator's input
        # and output budgets. Numeric/punctuation labels need no API request.
        for ci, chapter in enumerate(book.chapters):
            for pi, (source, korean) in enumerate(chapter.pairs):
                if not korean.strip() and not any(c.isalpha() for c in plain_text(source)):
                    chapter.pairs[pi] = (source, source)
            missing = [pi for pi, (_, ko) in enumerate(chapter.pairs) if not ko.strip()]
            missing_sources = [chapter.pairs[pi][0] for pi in missing]
            for offset, window in translate._plan_windows(missing_sources,
                    config.TRANSLATE_BATCH_PARAGRAPHS, config.TRANSLATE_BATCH_MAX_CHARS):
                keys = missing[offset:offset + len(window)]
                before = [s for s, _ in chapter.pairs[max(0, keys[0] - 3):keys[0]]]
                translations = translate._translate_batch(window, entry["language"],
                    precision.flat_glossary(sheet), before, sheet)
                if len(translations) != len(keys) or any(not ko.strip() for ko in translations):
                    checkpoint("draft_failed")
                    raise translate.TranslationError(f"recovered source draft empty: chapter {ci}")
                for pi, korean in zip(keys, translations):
                    chapter.pairs[pi] = (chapter.pairs[pi][0], korean)
                checkpoint("drafting_restored_text")
        profile = {"model": config.ZAI_MODEL, "quality_profile": sheet["profile_hash"],
                   "source_sha256": baseline["source_sha256"], "review_version": VERSION}
        result_path = directory / "result.json"
        if state.get("stage") == "completed" and result_path.exists():
            previous_result = json.loads(result_path.read_text())
            output_path = Path(previous_result["output"])
            if (previous_result["quality_report"].get("passed")
                    and not find_translation_integrity_violations(book)
                    and all(_semantic_signature(r, r["current_ko"]) in verified
                            for r in _semantic_rows(book, None, profile))
                    and output_path.exists()
                    and digest(output_path.read_bytes()) == previous_result["sha256"]):
                validate_epub(str(output_path), external=False,
                              expected_content=[p for c in book.chapters for p in c.pairs])
                print(f"[{entry['id']}] completed review reused after exact-text/profile/artifact checks", flush=True)
                return previous_result
        checkpoint()
        book.quality_report = audit_book_quality(book, sheet, chat, verified=verified,
            progress_cb=checkpoint, audit_profile=profile)
        checkpoint("approved")
        content = [p for c in book.chapters for p in c.pairs]
        baseline_sources = [s for c in baseline["chapters"] for s, _ in c["pairs"]]
        if baseline_sources != [s for s, _ in content]:
            raise ValueError("review changed source text")
        changes = []
        for ci, chapter in enumerate(book.chapters):
            for pi, (source, korean) in enumerate(chapter.pairs):
                before = baseline["chapters"][ci]["pairs"][pi][1]
                if before != korean:
                    changes.append({"chapter": ci, "paragraph": pi, "source": source,
                                    "before": before, "after": korean,
                                    "restored": not before.strip()})
        output = root / "epub" / entry["output"]
        report = {}
        payload = {**serialize(book), "source_lang": entry["language"], "provenance": {
            "source": entry["source_name"], "url": entry["source_url"], "rights": entry["rights"],
            "source_sha256": baseline["source_sha256"], "editorial_version": VERSION,
            "semantic_audit_version": SEMANTIC_AUDIT_VERSION, "quality_profile": sheet["profile_hash"]}}
        # Keep the existing cover rather than fetching a new unrelated image.
        cover = None
        with zipfile.ZipFile(path_of(entry["inputs"][0])) as archive:
            for name in archive.namelist():
                if name.lower().endswith(("cover.jpg", "cover.jpeg", "cover.png", "cover.gif")):
                    cover = archive.read(name)
                    break
        assemble.assemble(payload, str(output), source_lang=entry["language"],
                          cover_bytes=cover, validation_report=report)
        local = validate_epub(str(output), expected_pairs=len(content),
                              external=False, expected_content=content)
        report["content_compared"] = local["content_compared"]
        save(directory / "changes.json", changes)
        summary = {"id": entry["id"], "title": book.title, "inputs": baseline["input_hashes"],
                   "output": str(output), "sha256": digest(output.read_bytes()), "pairs": len(content),
                   "changed_or_restored_pairs": len(changes), "coverage": baseline["coverage"],
                   "quality_report": book.quality_report, "epub_validation": report, "calls": calls}
        save(directory / "result.json", summary)
        checkpoint("completed")
        print(json.dumps({"completed": entry["id"], "pairs": len(content),
                          "changes": len(changes), "output": str(output)}, ensure_ascii=False), flush=True)
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--edition", action="append")
    args = parser.parse_args()
    manifest, root = prepare(path_of(args.manifest))
    if args.prepare_only:
        return
    targets = [e for e in manifest["editions"] if not args.edition or e["id"] in args.edition]
    errors = []
    with ThreadPoolExecutor(max_workers=min(args.workers, config.TRANSLATE_WORKERS)) as executor:
        futures = {executor.submit(review_one, e, root): e["id"] for e in targets}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as error:
                errors.append({"id": futures[future], "error": str(error)})
                save(root / futures[future] / "error.json", errors[-1])
                print(json.dumps({"failed": futures[future], "error": str(error)}, ensure_ascii=False), flush=True)
    save(root / "run-errors.json", errors)
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
