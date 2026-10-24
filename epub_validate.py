"""Deterministic publication checks, with EPUBCheck when installed.

The built-in checks are a publication gate, not a replacement for the complete
W3C conformance suite. Set EPUBCHECK_JAR or install `epubcheck` to run that suite.
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import shutil
import subprocess
import zipfile
from urllib.parse import unquote, urlsplit

from lxml import etree

import config
from epub_markup import XHTML_NS, plain_text

OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"


class EpubValidationError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise EpubValidationError(message)


def _resolve(base: str, reference: str) -> tuple[str, str]:
    url = urlsplit(reference)
    _require(not url.scheme and not url.netloc, f"unexpected external resource: {reference}")
    path = unquote(url.path)
    target = posixpath.normpath(posixpath.join(posixpath.dirname(base), path)) if path else base
    _require(not target.startswith(("../", "/")) and "\\" not in target,
             f"unsafe resource reference: {reference}")
    return target, unquote(url.fragment)


def validate_epub(path: str, expected_pairs: int | None = None,
                  external: bool = True,
                  expected_content: list[tuple[str, str]] | None = None) -> dict:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        _require(len(names) == len(set(names)), "duplicate ZIP entries")
        _require(bool(names) and names[0] == "mimetype", "mimetype must be the first ZIP entry")
        _require(archive.getinfo("mimetype").compress_type == zipfile.ZIP_STORED,
                 "mimetype must be uncompressed")
        _require(archive.read("mimetype") == b"application/epub+zip", "invalid EPUB mimetype")
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        trees = {}
        for name in names:
            _require(not name.startswith("/") and ".." not in name.split("/") and "\\" not in name,
                     f"unsafe ZIP path: {name}")
            if name.endswith((".xml", ".opf", ".xhtml", ".ncx", ".svg")):
                try:
                    trees[name] = etree.fromstring(archive.read(name), parser)
                except etree.XMLSyntaxError as exc:
                    raise EpubValidationError(f"invalid XML in {name}: {exc}") from exc
                _require(not trees[name].getroottree().docinfo.internalDTD
                         or not trees[name].getroottree().docinfo.internalDTD.entities(),
                         f"custom entities are unsupported: {name}")
        container = trees.get("META-INF/container.xml")
        _require(container is not None, "missing container.xml")
        roots = container.xpath('//*[local-name()="rootfile"]/@full-path')
        _require(len(roots) == 1 and roots[0] in trees, "missing/ambiguous package document")
        opf_name = roots[0]
        package = trees[opf_name]
        _require(package.tag == f"{{{OPF}}}package", "invalid package namespace")
        for field in ("identifier", "title", "language"):
            _require(bool(package.findall(f".//{{{DC}}}{field}")), f"missing dc:{field}")
        manifest = {}
        resources = set()
        navs = []
        for item in package.findall(f"{{{OPF}}}manifest/{{{OPF}}}item"):
            item_id, href = item.get("id"), item.get("href", "")
            _require(item_id and item_id not in manifest, "duplicate/missing manifest id")
            target, _ = _resolve(opf_name, href)
            _require(target in names, f"manifest resource missing: {target}")
            _require(target not in resources, f"duplicate manifest href: {target}")
            resources.add(target)
            manifest[item_id] = target
            media = item.get("media-type")
            data = archive.read(target)
            if media == "image/png":
                _require(data.startswith(b"\x89PNG\r\n\x1a\n"), f"PNG media mismatch: {target}")
            elif media == "image/jpeg":
                _require(data.startswith(b"\xff\xd8\xff"), f"JPEG media mismatch: {target}")
            if "nav" in item.get("properties", "").split():
                navs.append(target)
        _require(len(navs) == 1, "EPUB needs exactly one navigation document")
        _require(set(names) - {"mimetype", "META-INF/container.xml", opf_name} <= resources,
                 "packaged resource omitted from manifest")
        spine = package.findall(f"{{{OPF}}}spine/{{{OPF}}}itemref")
        _require(bool(spine), "empty spine")
        for item in spine:
            _require(item.get("idref") in manifest, "unresolved spine idref")
        ids = {}
        originals = translations = 0
        for name, root in trees.items():
            node_ids = root.xpath('//@id')
            _require(len(node_ids) == len(set(node_ids)), f"duplicate IDs in {name}")
            ids[name] = set(node_ids)
            is_xhtml = name.endswith(".xhtml")
            if not is_xhtml and not name.endswith(".svg"):
                continue
            if is_xhtml:
                _require(root.tag == f"{{{XHTML_NS}}}html", f"invalid XHTML namespace: {name}")
            for node in root.iter():
                if not isinstance(node.tag, str):
                    continue
                tag = etree.QName(node).localname
                _require(tag not in {"script", "iframe", "object", "embed", "form"},
                         f"active content is unsupported: {name}/{tag}")
                _require(not any(key.lower().startswith("on") for key in node.attrib),
                         f"event handler in {name}")
                _require("data-bf-unit" not in node.attrib, "unassembled translation slot")
                if not is_xhtml:
                    continue
                classes = node.get("class", "").split()
                if config.ORIGINAL_CLASS in classes:
                    originals += 1
                    sibling = node.getnext()
                    _require(sibling is not None and config.TRANSLATION_CLASS in sibling.get("class", "").split(),
                             f"unpaired source block: {name}")
                    _require(bool(plain_text(etree.tostring(node, encoding="unicode"))),
                             f"blank source: {name}")
                if config.TRANSLATION_CLASS in classes:
                    translations += 1
                    _require(bool(plain_text(etree.tostring(node, encoding="unicode"))),
                             f"blank translation: {name}")
        for name, root in trees.items():
            for node in root.iter():
                if not isinstance(node.tag, str):
                    continue
                for key, ref in node.attrib.items():
                    attr = etree.QName(key).localname
                    if attr not in {"href", "src", "poster"}:
                        continue
                    if not ref:
                        continue
                    url = urlsplit(ref)
                    if url.scheme or url.netloc:
                        _require(attr == "href" and etree.QName(node).localname == "a"
                                 and url.scheme in {"http", "https", "mailto"},
                                 f"unsupported remote/active resource: {ref}")
                        continue
                    target, fragment = _resolve(name, ref)
                    _require(target in names, f"broken resource link in {name}: {ref}")
                    if fragment:
                        _require(fragment in ids.get(target, set()), f"broken anchor in {name}: {ref}")
        # Compare ordered final reader-visible text, not merely equal counts.
        # Read spine order: ZIP entry ordering is not publication reading order.
        if expected_content is not None:
            actual = []
            for item in spine:
                root = trees.get(manifest[item.get("idref")])
                if root is None:
                    continue
                for node in root.iter():
                    if config.ORIGINAL_CLASS in node.get("class", "").split():
                        actual.append((plain_text(etree.tostring(node, encoding="unicode")),
                                       plain_text(etree.tostring(node.getnext(), encoding="unicode"))))
            wanted = [(plain_text(src), plain_text(ko)) for src, ko in expected_content]
            _require(actual == wanted,
                     "assembled source/translation text or reading order differs from approved book")
        _require(originals == translations, "source/translation block count mismatch")
        if expected_pairs is not None:
            _require(originals == expected_pairs, "assembled pair count differs from translated book")
    checker = "not_available"
    if external:
        jar = os.environ.get("EPUBCHECK_JAR")
        bundled = os.path.join(config.DATA_DIR, "tools", "epubcheck-5.4.0", "epubcheck.jar")
        if not jar and os.path.isfile(bundled):
            jar = bundled
        binary = shutil.which("epubcheck")
        command = (["java", "-jar", jar] if jar else [binary] if binary else None)
        if command:
            result = subprocess.run(command + [path], capture_output=True, text=True, timeout=180)
            _require(result.returncode == 0,
                     "EPUBCheck failed: " + (result.stdout + result.stderr)[-6000:])
            checker = "passed"
        elif os.environ.get("BABELFORGE_REQUIRE_EPUBCHECK") == "1":
            raise EpubValidationError("EPUBCheck is required; set EPUBCHECK_JAR or install epubcheck")
    return {"passed": True, "pairs": originals, "epubcheck": checker,
            "content_compared": expected_content is not None}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("epub")
    print(json.dumps(validate_epub(ap.parse_args().epub), ensure_ascii=False))
