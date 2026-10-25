"""Verified revision promotion. No retained old-edition backups.

Replacement preserves a canonical Calibre ID. Obsolete files/records are removed
only after public acquisition verification and catalog commit. A small journal
contains hashes and progress, never a copy of an obsolete book.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import plistlib
import socket
import subprocess
import tempfile
import time
from urllib.parse import urljoin, urlsplit
import urllib.request

from lxml import etree

import catalog
import config
import publish
from epub_validate import validate_epub
from state_store import file_lock, _write


class PublicationError(RuntimeError):
    pass


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def content(path):
    # Delayed import avoids coupling generation to the review CLI at startup.
    from review_library import read_bilingual
    book = read_bilingual(Path(path))
    pairs = [pair for chapter in book.chapters for pair in chapter.pairs]
    if not pairs:
        raise PublicationError("Publication requires bilingual source/Korean content")
    return pairs


def fetch(url):
    # Some public reverse proxies reject Python's default UA, even for OPDS.
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=45) as response:
        if response.status != 200:
            raise PublicationError(f"Acquisition did not return HTTP 200: {url}")
        return response.read()


def opds_acquisitions():
    """Read Calibre's advertised newest view, preserving duplicate entries."""
    origin = config.OPDS_BASE_URL.rstrip("/")
    feed_url = config.OPDS_FEED_URL
    queue, visited, found = [feed_url], set(), {}
    while queue:
        url = queue.pop(0)
        if url in visited:
            continue
        if len(visited) >= 1000:
            raise PublicationError("OPDS traversal exceeded its bounded page limit")
        visited.add(url)
        tree = etree.fromstring(fetch(url), parser=etree.XMLParser(resolve_entities=False, no_network=True))
        entries = tree.findall('{http://www.w3.org/2005/Atom}entry')
        for link in tree.findall('{http://www.w3.org/2005/Atom}link'):
            if link.get('rel') == 'next':
                queue.append(_same_origin(url, link.get('href'), origin))
        for entry in entries:
            links = entry.findall('{http://www.w3.org/2005/Atom}link')
            for link in links:
                href = link.get('href', '')
                if 'acquisition' in link.get('rel', '') and link.get('type') == 'application/epub+zip':
                    parts = urlsplit(href).path.strip('/').split('/')
                    if len(parts) < 3 or parts[0] != 'get' or parts[1].lower() != 'epub' or not parts[2].isdigit():
                        raise PublicationError("Unrecognized OPDS EPUB acquisition")
                    found.setdefault(int(parts[2]), []).append(_same_origin(url, href, origin))
                elif '4f6e6577657374' in href:  # Calibre's advertised By Newest view
                    queue.append(_same_origin(url, href, origin))
    return found


def verify_public(book_id, expected, directory):
    """Follow advertised links with pagination; require this ID exactly once."""
    found = opds_acquisitions().get(book_id, [])
    if len(found) != 1:
        raise PublicationError(f"OPDS must advertise record {book_id} exactly once, got {len(found)}")
    path = Path(directory) / 'acquisition.epub'
    path.write_bytes(fetch(found[0]))
    report = validate_epub(str(path), expected_content=expected, expected_pairs=len(expected))
    if report['epubcheck'] != 'passed':
        raise PublicationError("Official EPUBCheck is required for published acquisitions")
    stable = Path(directory) / 'stable.epub'
    stable.write_bytes(fetch(publish.opds_url_for(book_id)))
    # Calibre may rewrite metadata, so compare visible content rather than ZIP bytes.
    stable_report = validate_epub(str(stable), expected_content=expected, expected_pairs=len(expected))
    if stable_report['epubcheck'] != 'passed':
        raise PublicationError("Official EPUBCheck is required for stable acquisitions")
    return {"acquisition_url": found[0], "sha256": digest(path), "validation": report,
            "stable_validation": stable_report}


def _same_origin(base, href, origin):
    target = urljoin(base, href)
    a, b = urlsplit(target), urlsplit(origin)
    if (a.scheme, a.netloc) != (b.scheme, b.netloc):
        raise PublicationError("Refusing cross-origin OPDS acquisition/navigation")
    return target


def _launch(*args):
    return subprocess.run(['launchctl', *args], check=True, capture_output=True, text=True, timeout=30)


def _managed_server():
    plist = Path(config.CALIBRE_LAUNCH_AGENT)
    endpoint = urlsplit(config.CALIBREDB_URL)
    if publish.calibre_target.get() or not plist.is_file() or endpoint.hostname not in {'localhost', '127.0.0.1', '::1'}:
        return None
    settings = plistlib.loads(plist.read_bytes())
    args = settings.get('ProgramArguments', [])
    if '--disable-local-write' not in args:
        return None
    library = str(Path(config.CALIBRE_LIBRARY).resolve())
    if library not in args or settings.get('Label') != plist.stem:
        raise PublicationError("Managed server/library mismatch; refusing offline writes")
    if '--port' not in args or args[args.index('--port') + 1] != str(endpoint.port or 80):
        raise PublicationError("Managed server port does not match publication endpoint")
    return {"plist": str(plist.resolve()), "sha256": digest(plist),
            "domain": f'gui/{os.getuid()}', "label": settings['Label'],
            "library": library, "host": endpoint.hostname, "port": endpoint.port or 80}


def _wait_server(info, running):
    for _ in range(80 if running else 40):
        try:
            with socket.create_connection((info['host'], info['port']), timeout=.5):
                pass
        except ConnectionRefusedError:
            if not running:
                return
        except OSError:
            if not running:
                raise PublicationError("Cannot prove server stopped; refusing database writes")
        else:
            if running:
                return
        time.sleep(.25)
    raise PublicationError("Publication server did not " + ('restart' if running else 'stop'))


def _maintenance_marker():
    return Path(config.DATA_DIR) / '.calibre-maintenance.json'


def _recover_server(info):
    marker = _maintenance_marker()
    if not marker.exists():
        return
    if info is None or json.loads(marker.read_text()) != info:
        raise PublicationError("Pending server recovery does not match current configuration")
    try:
        _launch('print', f'{info["domain"]}/{info["label"]}')
    except subprocess.CalledProcessError:
        _wait_server(info, running=False)
        _launch('bootstrap', info['domain'], info['plist'])
    _wait_server(info, running=True)
    marker.unlink()


def recover_calibre_server():
    """Restore only a service this publisher recorded as interrupted."""
    with file_lock(str(Path(config.DATA_DIR) / '.calibre-maintenance.lock')):
        _recover_server(_managed_server())


@contextmanager
def calibre_session():
    """Never grant anonymous writes. Recover a stopped service after a crash."""
    info = _managed_server()
    if info is None:
        yield
        return
    with file_lock(str(Path(config.DATA_DIR) / '.calibre-maintenance.lock')):
        _recover_server(info)
        _launch('print', f'{info["domain"]}/{info["label"]}')
        # Persist before unloading: a killed process cannot run finally.
        _write(str(_maintenance_marker()), info)
        token = None
        try:
            _launch('bootout', info['domain'], info['plist'])
            _wait_server(info, running=False)
            token = publish.calibre_target.set(info['library'])
            yield
        finally:
            if token is not None:
                publish.calibre_target.reset(token)
            _recover_server(info)


def _run(*args):
    return publish._run(publish.db_command(*args))


def _owned_path(path):
    p = Path(path)
    root = Path(config.LIBRARY_DIR).resolve()
    if p.is_symlink() or not p.resolve().is_relative_to(root) or p.suffix.lower() != '.epub':
        raise PublicationError(f"Refusing cleanup outside the managed EPUB library: {p}")
    return p


def _atomic_copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=dst.parent, suffix='.epub.tmp')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(Path(src).read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, dst)
    finally:
        Path(temporary).unlink(missing_ok=True)


def publish_revision(epub_path, title, author, *, request_id=None, **options):
    """Own the request during CLI publication, or reuse the pipeline's lock."""
    if request_id:
        entry = catalog.get(request_id)
        if entry is None:
            raise PublicationError("Unknown publication request")
        if not (entry.get('worker_pid') == os.getpid() and catalog._worker_locked(request_id)):
            with catalog.request_worker(request_id):
                return _publish_revision(epub_path, title, author, request_id=request_id, **options)
    return _publish_revision(epub_path, title, author, request_id=request_id, **options)


def _publish_revision(epub_path, title, author, *, source='', rights='', source_id='',
                     source_lang='en', request_id=None, destination=None,
                     obsolete_files=None, legacy_id=None, legacy_path=None,
                     legacy_sha256=None, expected_sha256=None, expected_content=None,
                     source_sha256=None):
    """Publish once, verify public bytes, commit, then delete known old copies.

    obsolete_files is an explicit path→SHA256 map, never a title-based glob.
    A legacy record needs an explicit local artifact hash AND exact content match.
    """
    original = Path(epub_path).resolve()
    identifier = publish.edition_identifier(str(original), source, source_id, source_lang)
    if destination is None and not original.is_relative_to(Path(config.LIBRARY_DIR).resolve()):
        destination = Path(config.LIBRARY_DIR) / f"revision-{identifier[:16]}.epub"
    destination = _owned_path(destination or original)
    journal_dir = Path(config.DATA_DIR) / 'publications'
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal_path = journal_dir / (identifier + '.json')
    with file_lock(str(Path(config.DATA_DIR) / '.publication.lock')):
        recover_calibre_server()
        previous = json.loads(journal_path.read_text()) if journal_path.exists() else {}
        obsolete = dict(previous.get('obsolete_files', {}))
        obsolete.update(obsolete_files or {})
        # Discover earlier *identified* successful requests, never similarly named editions.
        records = catalog._load()['entries']
        if request_id and not any(e['id'] == request_id for e in records):
            raise PublicationError("Unknown publication request")
        current = next((e for e in records if e['id'] == request_id), None)
        if current and catalog._busy(current) and current.get('worker_pid') != os.getpid():
            raise PublicationError("Another worker owns the publication request")
        def same_edition(entry):
            if entry.get('publication_identifier'):
                return entry['publication_identifier'] == identifier
            selected = entry.get('selected') or {}
            return bool(source_id and selected.get('source') == source and
                        str(selected.get('source_id', '')) == str(source_id) and
                        selected.get('language') == source_lang)
        matching = [e for e in records if same_edition(e)]
        matching_request_ids = {e['id'] for e in matching}
        source_sha256 = source_sha256 or (current or {}).get('source_sha256')
        if source == 'local' and os.path.isabs(str(source_id)) and source_sha256:
            known = [e.get('source_sha256') for e in matching if e['id'] != request_id]
            known.append(previous.get('source_sha256'))
            if any(value and value != source_sha256 for value in known):
                raise PublicationError("Local source path now contains another edition; use a distinct source_id")
        if current and (current.get('publication_identifier') or current.get('selected')) and not same_edition(current):
            raise PublicationError("Request belongs to another source edition")
        for entry in records:
            if (entry.get('epub_path') and Path(entry['epub_path']).resolve() == destination.resolve()
                    and entry['id'] != request_id and not same_edition(entry)):
                raise PublicationError("Destination is owned by another source edition")
        for entry in matching:
            if entry.get('status') not in {'completed', 'published'} or catalog._busy(entry):
                continue
            old = entry.get('epub_path')
            if old and Path(old).resolve() != destination.resolve() and entry.get('artifact_sha256'):
                obsolete.setdefault(old, entry['artifact_sha256'])
        for old, expected_hash in obsolete.items():
            p = _owned_path(old)
            if p.resolve() == destination.resolve() or p.resolve() == original:
                raise PublicationError("Current input/destination cannot be an obsolete file")
            if p.exists() and digest(p) != expected_hash:
                raise PublicationError(f"Obsolete file changed; refusing deletion: {p}")
            if any(e.get('epub_path') and Path(e['epub_path']).resolve() == p.resolve()
                   and e['id'] not in matching_request_ids and
                   e.get('status') in {'published','completed'} for e in records):
                raise PublicationError("Obsolete path is also used by another edition")
        with tempfile.TemporaryDirectory(prefix='babelforge-publish-') as work:
            frozen = Path(work) / 'approved.epub'
            frozen.write_bytes(original.read_bytes())
            if expected_sha256 and digest(frozen) != expected_sha256:
                raise PublicationError("Artifact changed after editorial validation")
            expected = expected_content if expected_content is not None else content(frozen)
            local_report = validate_epub(str(frozen), expected_content=expected, expected_pairs=len(expected))
            if local_report['epubcheck'] != 'passed':
                raise PublicationError("Official EPUBCheck required before publication")
            state = {"identifier": identifier, "artifact_sha256": digest(frozen),
                     "destination": str(destination), "obsolete_files": obsolete,
                     "stage": "validated", "source": source, "source_id": source_id,
                     "source_lang": source_lang, "source_sha256": source_sha256}
            _write(str(journal_path), state)
            legacy_proven = False
            if legacy_id is not None and not publish._matching_ids(identifier):
                if not legacy_path or digest(legacy_path) != legacy_sha256:
                    raise PublicationError("Legacy binding needs a verified exact local artifact")
                old_download = Path(work) / 'legacy.epub'
                old_download.write_bytes(fetch(publish.opds_url_for(legacy_id)))
                validate_epub(str(old_download), external=False,
                              expected_content=content(legacy_path))
                legacy_proven = True
            with calibre_session():
                ids = sorted(set(publish._matching_ids(identifier)))
                if not ids and legacy_id is not None:
                    assert legacy_proven
                    rows = json.loads(_run('list', '--for-machine', '--fields', 'identifiers',
                                           '--search', f'id:={legacy_id}'))
                    if len(rows) != 1 or rows[0]['id'] != legacy_id:
                        raise PublicationError("Legacy record disappeared")
                    identifiers = rows[0].get('identifiers') or {}
                    if identifiers.get('babelforge') not in (None, identifier):
                        raise PublicationError("Legacy record belongs to another edition")
                    identifiers['babelforge'] = identifier
                    _run('set_metadata', str(legacy_id), '--field',
                         'identifiers:' + ','.join(f'{k}:{v}' for k,v in identifiers.items()))
                    ids = [legacy_id]
                canonical = previous.get('book_id') if previous.get('book_id') in ids else (ids[0] if ids else None)
                book_id = publish.add_to_library(str(frozen), title, author, source, rights,
                    source_id=source_id, source_lang=source_lang, preferred_id=canonical)
                state.update(book_id=book_id, duplicate_ids=[i for i in ids if i != book_id], stage='uploaded')
                _write(str(journal_path), state)
            verification = verify_public(book_id, expected, work)
            state.update(verification=verification, stage='verified')
            _write(str(journal_path), state)
            _atomic_copy(frozen, destination)
            with catalog._transaction() as data:
                if request_id:
                    current = next(e for e in data['entries'] if e['id'] == request_id)
                    current.update(status='publishing', publication_stage='cleanup_pending', epub_path=str(destination),
                        opds_url=publish.opds_url_for(book_id), calibre_id=book_id,
                        publication_identifier=identifier, artifact_sha256=digest(frozen),
                        publication_verification=verification, error=None,
                        progress={'done':100,'total':100,'pct':100.0}, updated_ms=int(time.time()*1000))
            state['stage'] = 'catalog_committed'
            _write(str(journal_path), state)
            if state['duplicate_ids']:
                with calibre_session():
                    live = set(publish._matching_ids(identifier))
                    if book_id not in live or not set(state['duplicate_ids']).issubset(live):
                        raise PublicationError("Duplicate identity changed before cleanup")
                    # Delete only redundant records; canonical ID is never removed.
                    _run('remove', '--permanent', ','.join(map(str, state['duplicate_ids'])))
                verify_public(book_id, expected, work)
            with catalog._transaction() as data:
                # Recheck live references while holding the same lock as catalog
                # writers: a later request must not turn our cleanup into data loss.
                for old, expected_hash in obsolete.items():
                    p = _owned_path(old)
                    if any(e.get('epub_path') and Path(e['epub_path']).resolve() == p.resolve()
                           and (not same_edition(e) or catalog._busy(e)) for e in data['entries']
                           if e['id'] != request_id):
                        raise PublicationError("Old file acquired another live owner; cleanup stopped")
                    if p.exists():
                        if digest(p) != expected_hash:
                            raise PublicationError("Old file changed after verification; cleanup stopped")
                        p.unlink()
                # Mark success only after cleanup. A failure remains retryable.
                if request_id:
                    current = next(e for e in data['entries'] if e['id'] == request_id)
                    current.update(status='published', publication_stage='completed')
                data['entries'] = [e for e in data['entries'] if not (
                    request_id and e['id'] != request_id and same_edition(e) and
                    e.get('status') in {'published','completed'} and not catalog._busy(e))]
            state.update(stage='completed', opds_url=publish.opds_url_for(book_id),
                         validation=local_report, obsolete_files={})
            _write(str(journal_path), state)
            return state
