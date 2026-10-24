"""Validate completed library reviews and package their standard EPUB delivery.

Only layout/metadata changes are allowed here; approved Korean and normalized
visible source text must remain identical. Never bypass incomplete editorial work.
Explicit --publish chains verified revision promotion after local packaging.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import posixpath
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag

from lxml import etree

import config
from epub_markup import XHTML_NS, XML_NS, plain_text
from epub_validate import validate_epub
from quality_audit import (_semantic_rows, _semantic_signature,
                           find_translation_integrity_violations)
from review_library import (BASE, VERSION, deserialize, digest, normalized, path_of, save,
                            load_review_sheet, checked_baseline)


NS = {"x": XHTML_NS, "o": "http://www.idpf.org/2007/opf",
      "dc": "http://purl.org/dc/elements/1.1/", "n": "http://www.daisy.org/z3986/2005/ncx/"}


def _source_stanzas(source: str):
    """Recognize explicit indented source blocks, not inferred verse by meaning."""
    candidates = {}
    for block in re.split(r"\n[ \t]*\n", source):
        lines = [line for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or not all(re.match(r"[ \t]{3,}\S", line) for line in lines):
            continue
        text = "\n".join(lines)
        candidates.setdefault(normalized(text), set()).add(text)
    return {key: next(iter(values)) for key, values in candidates.items() if len(values) == 1}


def restore_source_linebreaks(tree, stanzas):
    groups = {}
    for node in tree.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," bf-original ")]'):
        parent = node.getparent()
        while parent is not None and etree.QName(parent).localname not in {"p", "pre", "blockquote", "h1", "h2", "h3"}:
            parent = parent.getparent()
        if parent is not None:
            groups.setdefault(parent, []).append(node)
    count = 0
    for parent, nodes in groups.items():
        if any(len(node) for node in nodes):
            continue  # never flatten emphasis/links to restore whitespace
        key = "".join(normalized(node.text or "") for node in nodes)
        raw = stanzas.get(key)
        if not raw:
            continue
        positions = [i for i, char in enumerate(raw) if not char.isspace()]
        offset = 0
        for node in nodes:
            length = len(normalized(node.text or ""))
            end = offset + length
            left = 0 if offset == 0 else positions[offset]
            right = len(raw) if end == len(positions) else positions[end]
            node.text = raw[left:right]
            offset = end
        if offset != len(positions):
            raise ValueError("verse whitespace restoration changed source coverage")
        classes = parent.get("class", "").split()
        if "verse" not in classes:
            classes.append("verse")
        parent.set("class", " ".join(classes))
        count += 1
    return count


def _chapter_key(text, *, full=False):
    text = text.strip().strip("_*").strip()
    pattern = r"(?:CHAPTER|CHAP\.)\s+([IVXLCDM]+|\d+)"
    match = re.fullmatch(pattern + r"\.?", text, re.I) if full else re.match(pattern + r"\b", text, re.I)
    return match.group(1).upper() if match else None


def add_unlisted_headings(trees, nav_name, ncx_name, spine_names):
    """Add chapter anchors overlooked by legacy TXT heading recognition."""
    nav = trees[nav_name]
    lists = nav.xpath('//*[local-name()="nav"][@*[local-name()="type"]="toc"]/*[local-name()="ol"]')
    if len(lists) != 1:
        raise ValueError("missing/ambiguous navigation list")
    listing = lists[0]
    entries = list(listing)
    known = {_chapter_key("".join(a.itertext())) for a in listing.xpath('.//*[local-name()="a"]')}
    added = 0
    for doc in spine_names:
        tree = trees.get(doc)
        if tree is None:
            continue
        for node in tree.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," bf-original ")]'):
            source = plain_text(etree.tostring(node, encoding="unicode", with_tail=False))
            key = _chapter_key(source, full=True)
            if not key or key in known:
                continue
            target = node.getnext()
            if target is None:
                raise ValueError("unpaired chapter heading")
            parent = node.getparent()
            # Headings restored as plain paragraphs can regain their semantics.
            if etree.QName(parent).localname == "p" and len(parent) == 2:
                parent.tag = f"{{{XHTML_NS}}}h1"
            existing_ids = set(tree.xpath('//@id'))
            anchor = parent.get("id") or "review-chapter-" + key.lower()
            while anchor in existing_ids and parent.get("id") != anchor:
                anchor += "-1"
            parent.set("id", anchor)
            li = etree.Element(f"{{{XHTML_NS}}}li")
            a = etree.SubElement(li, f"{{{XHTML_NS}}}a", href=
                posixpath.relpath(doc, posixpath.dirname(nav_name)) + "#" + anchor)
            original = etree.SubElement(a, f"{{{XHTML_NS}}}span", lang=node.get("lang", "en"))
            original.set(f"{{{XML_NS}}}lang", node.get("lang", "en"))
            original.text = source.strip("_*")
            original.tail = " ("
            korean = etree.SubElement(a, f"{{{XHTML_NS}}}span", lang="ko")
            korean.set(f"{{{XML_NS}}}lang", "ko")
            korean.text = plain_text(etree.tostring(target, encoding="unicode", with_tail=False))
            korean.tail = ")"
            entries.append(li)
            known.add(key)
            added += 1
    if not added:
        return 0
    ranks = {name: i for i, name in enumerate(spine_names)}
    def rank(li):
        a = li.xpath('.//*[local-name()="a"]')[0]
        document, anchor = urldefrag(a.get("href"))
        name = posixpath.normpath(posixpath.join(posixpath.dirname(nav_name), document))
        position = -1
        if anchor and name in trees:
            for i, n in enumerate(trees[name].iter()):
                if n.get("id") == anchor:
                    position = i
                    break
        return ranks.get(name, len(ranks)), position
    entries.sort(key=rank)
    for child in list(listing):
        listing.remove(child)
    listing.extend(entries)
    if ncx_name:
        ncx = trees[ncx_name]
        navmap = ncx.find("n:navMap", NS)
        for child in list(navmap):
            navmap.remove(child)
        for i, li in enumerate(entries, 1):
            a = li.xpath('.//*[local-name()="a"]')[0]
            point = etree.SubElement(navmap, f"{{{NS['n']}}}navPoint", id=f"review-nav-{i}", playOrder=str(i))
            label = etree.SubElement(point, f"{{{NS['n']}}}navLabel")
            etree.SubElement(label, f"{{{NS['n']}}}text").text = "".join(a.itertext())
            document, anchor = urldefrag(a.get("href"))
            name = posixpath.normpath(posixpath.join(posixpath.dirname(nav_name), document))
            href = posixpath.relpath(name, posixpath.dirname(ncx_name)) + ("#" + anchor if anchor else "")
            etree.SubElement(point, f"{{{NS['n']}}}content", src=href)
    return added


def refine(path: Path, entry, content):
    with zipfile.ZipFile(path) as archive:
        files = {n: archive.read(n) for n in archive.namelist()}
    container = etree.fromstring(files["META-INF/container.xml"])
    opfn = container.xpath('//*[local-name()="rootfile"]/@full-path')[0]
    opf = etree.fromstring(files[opfn])
    items = {i.get("id"): i for i in opf.findall("o:manifest/o:item", NS)}
    def item_path(item):
        return posixpath.normpath(posixpath.join(posixpath.dirname(opfn), item.get("href")))
    spine = [item_path(items[i.get("idref")]) for i in opf.findall("o:spine/o:itemref", NS)]
    nav_name = next(item_path(i) for i in items.values() if "nav" in i.get("properties", "").split())
    ncx_name = next((item_path(i) for i in items.values() if i.get("media-type") == "application/x-dtbncx+xml"), None)
    names = set(spine + [nav_name] + ([ncx_name] if ncx_name else []))
    trees = {name: etree.fromstring(files[name]) for name in names}
    stanzas = (_source_stanzas(path_of(entry["source_path"]).read_text(encoding="utf-8-sig"))
               if entry["source_format"] == "txt" else {})
    verse_count = sum(restore_source_linebreaks(trees[n], stanzas) for n in spine)
    headings = add_unlisted_headings(trees, nav_name, ncx_name, spine)
    metadata = opf.find("o:metadata", NS)
    if entry.get("title_language"):
        metadata.find("dc:title", NS).set(f"{{{XML_NS}}}lang", entry["title_language"])
    if entry.get("navigation_language"):
        language = entry["navigation_language"]
        for name, tree in trees.items():
            if not name.endswith((".xhtml", ".html")):
                continue
            title = tree.find("x:head/x:title", NS)
            if title is not None and re.search("[가-힣]", title.text or ""):
                title.set("lang", language)
                title.set(f"{{{XML_NS}}}lang", language)
            originals = tree.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," bf-original ")]')
            if not originals and name in spine:
                body = tree.find("x:body", NS)
                if body is not None:
                    body.set("lang", language)
                    body.set(f"{{{XML_NS}}}lang", language)
        for span in trees[nav_name].xpath('//*[local-name()="a"]/*[local-name()="span"]'):
            if re.search("[가-힣]", "".join(span.itertext())):
                span.set("lang", language)
                span.set(f"{{{XML_NS}}}lang", language)
    if entry.get("editorial_note"):
        notices = [trees[name].find("x:body", NS) for name in spine
                   if not trees[name].xpath('//*[contains(concat(" ",normalize-space(@class)," ")," bf-original ")]')
                   and plain_text(etree.tostring(trees[name], encoding="unicode"))]
        if not notices:
            raise ValueError("editorial note requested without an edition notice page")
        body = notices[0]
        note = body.find('x:p[@id="review-editorial-note"]', NS)
        if note is None:
            note = etree.SubElement(body, f"{{{XHTML_NS}}}p", id="review-editorial-note", lang="ko")
            note.set(f"{{{XML_NS}}}lang", "ko")
        note.text = entry["editorial_note"]
    if entry.get("source_url"):
        sources = metadata.findall("dc:source", NS)
        source = sources[0] if sources else etree.SubElement(metadata, f"{{{NS['dc']}}}source")
        source.text = entry["source_url"]
    if entry.get("attribution"):
        rights = metadata.find("dc:rights", NS)
        if rights is None:
            rights = etree.SubElement(metadata, f"{{{NS['dc']}}}rights")
        rights.text = entry["rights"] + "; " + entry["attribution"]
    modified = metadata.find('o:meta[@property="dcterms:modified"]', NS)
    modified.text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fields = {"editorial_review": VERSION}
    if entry.get("source_name"):
        fields["source"] = entry["source_name"]
    for key, value in fields.items():
        meta = metadata.find(f'o:meta[@property="bf:{key}"]', NS)
        if meta is None:
            meta = etree.SubElement(metadata, f"{{{NS['o']}}}meta", property="bf:" + key)
        meta.text = value
    trees[opfn] = opf
    for name, tree in trees.items():
        files[name] = etree.tostring(tree, encoding="utf-8", xml_declaration=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".epub")
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("mimetype", files.pop("mimetype"), compress_type=zipfile.ZIP_STORED)
            for name, data in files.items():
                archive.writestr(name, data)
        report = validate_epub(temporary, expected_pairs=len(content), expected_content=content)
        if report["epubcheck"] != "passed":
            raise ValueError("final library delivery requires official EPUBCheck")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"source_blocks_with_restored_linebreaks": verse_count,
            "added_chapter_navigation_entries": headings}, report


def quotation_losses(book, baseline):
    """Flag newly lost quote boundaries; count differences are evidence to
    inspect, never authority to mechanically rewrite a legitimate Korean style.
    """
    pattern = re.compile('["“”„‟«»「」『』]')
    flags = []
    for ci, chapter in enumerate(book.chapters):
        for pi, (source, korean) in enumerate(chapter.pairs):
            before = baseline["chapters"][ci]["pairs"][pi][1]
            source_count = len(pattern.findall(plain_text(source)))
            old_count = len(pattern.findall(plain_text(before)))
            count = len(pattern.findall(plain_text(korean)))
            if source_count and (not before.strip() or old_count == source_count) and count < source_count:
                flags.append({"chapter": ci, "paragraph": pi,
                              "source": plain_text(source), "before": plain_text(before),
                              "after": plain_text(korean), "final_ko_sha256": digest(korean.encode())})
    return flags


def abbreviation_boundary_repetitions(book):
    """Catch a legacy Mr./Mrs. split translated twice across adjacent slots."""
    flags = []
    for ci, chapter in enumerate(book.chapters):
        for pi, ((source, korean), (next_source, next_ko)) in enumerate(
                zip(chapter.pairs, chapter.pairs[1:])):
            if not re.search(r"\b(?:Mr|Mrs|Ms|Dr|Prof)\.$", plain_text(source).strip().rstrip("_")):
                continue
            before = re.findall(r"[가-힣A-Za-z0-9]+", plain_text(korean))
            after = re.findall(r"[가-힣A-Za-z0-9]+", plain_text(next_ko))
            if before and after and len(before[-1]) >= 3 and before[-1] == after[0]:
                flags.append({"chapter": ci, "paragraph": pi, "next_paragraph": pi + 1,
                              "repeated_word": before[-1], "source": source,
                              "next_source": next_source, "korean": korean, "next_korean": next_ko})
    return flags


def finish(manifest_path):
    manifest = json.loads(path_of(manifest_path).read_text())
    root = path_of(manifest["review_dir"])
    results = []
    for entry in manifest["editions"]:
        if not (root / entry["id"] / "result.json").exists():
            raise ValueError(f"editorial review incomplete: {entry['id']}")
    for entry in manifest["editions"]:
        directory = root / entry["id"]
        if not (directory / "result.json").exists():
            raise ValueError(f"editorial review incomplete: {entry['id']}")
        state = json.loads((directory / "checkpoint.json").read_text())
        baseline = checked_baseline(directory, state)
        result = json.loads((directory / "result.json").read_text())
        if digest(path_of(entry["source_path"]).read_bytes()) != baseline["source_sha256"]:
            raise ValueError(f"retained source changed: {entry['id']}")
        sheet = load_review_sheet(directory)
        book = deserialize(state["book"])
        profile = {"model": config.ZAI_MODEL, "quality_profile": sheet["profile_hash"],
                   "source_sha256": baseline["source_sha256"], "review_version": VERSION}
        verified = set(state["verified"])
        if find_translation_integrity_violations(book):
            raise ValueError(f"translation integrity violations remain: {entry['id']}")
        if (state["stage"] != "completed" or not result["quality_report"]["passed"] or
                not all(_semantic_signature(r, r["current_ko"]) in verified
                        for r in _semantic_rows(book, None, profile))):
            raise ValueError(f"exact final approval incomplete: {entry['id']}")
        repetitions = abbreviation_boundary_repetitions(book)
        save(directory / "boundary-repetition-flags.json", repetitions)
        if repetitions:
            raise ValueError(f"legacy abbreviation boundary repeats Korean: {entry['id']}; "
                             f"{[(f['chapter'], f['paragraph']) for f in repetitions]}")
        flags = quotation_losses(book, baseline)
        save(directory / "quotation-boundary-flags.json", flags)
        decisions_path = directory / "quotation-review.json"
        decisions = json.loads(decisions_path.read_text()) if decisions_path.exists() else []
        resolved = {(r.get("chapter"), r.get("paragraph"), r.get("final_ko_sha256"))
                    for r in decisions if r.get("decision") == "valid-source-grounded-style"
                    and isinstance(r.get("reason"), str) and r["reason"].strip()}
        unresolved = [f for f in flags if (f["chapter"], f["paragraph"], f["final_ko_sha256"]) not in resolved]
        if unresolved:
            raise ValueError(f"quotation boundaries need source review: {entry['id']}; "
                             f"{[(f['chapter'], f['paragraph']) for f in unresolved]}")
        path = Path(result["output"])
        if digest(path.read_bytes()) != result["sha256"]:
            raise ValueError(f"output changed outside review: {path}")
        formatting, validation = refine(path, entry, [p for c in book.chapters for p in c.pairs])
        result.update({"sha256": digest(path.read_bytes()), "formatting": formatting,
                       "epub_validation": validation})
        save(directory / "result.json", result)
        results.append(result)
    output_dir = Path(config.LIBRARY_DIR) / ("reviewed-" + root.name.removeprefix("library-"))
    output_dir.mkdir(parents=True, exist_ok=True)
    for result in results:
        source = Path(result["output"])
        target = output_dir / source.name
        shutil.copyfile(source, target)
        result["delivery_path"] = str(target)
    bundle = output_dir / "전체-대조교정본.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for result in results:
            archive.write(result["delivery_path"], Path(result["delivery_path"]).name)
    summary = {"completed": True, "input_files": sum(len(e["inputs"]) for e in manifest["editions"]),
               "editions": results, "pairs": sum(r["pairs"] for r in results),
               "changed_or_restored_pairs": sum(r["changed_or_restored_pairs"] for r in results),
               "bundle": str(bundle), "bundle_sha256": digest(bundle.read_bytes())}
    save(root / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--publish", action="store_true",
                        help="Publish verified revisions and delete obsolete copies after OPDS checks")
    args = parser.parse_args()
    finish(args.manifest)
    if args.publish:
        from publish_reviewed import run
        run(args.manifest)
