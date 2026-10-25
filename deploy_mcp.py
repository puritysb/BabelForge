"""Deploy the managed MCP only after validating the complete reviewed library.

The review manifest is the inventory contract, not a fixed list of book IDs.
No translation, approval, publication, deletion or server-policy edits occur here.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile
import time
import uuid
import zipfile

import catalog
import config
from epub_validate import validate_epub
import publication
import publish
from publish_reviewed import checked_review, publication_options
from quality_audit import SEMANTIC_AUDIT_VERSION
from review_library import deserialize, path_of, save
from state_store import file_lock


class DeploymentError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise DeploymentError(message)


def git_state(expected_commit=None, clean=True):
    def git(*args):
        output = subprocess.check_output(['git', '-C', config.BASE_DIR, *args], text=True)
        return output if args[0] == 'ls-files' else output.strip()
    head = git('rev-parse', 'HEAD')
    dirty = bool(git('status', '--porcelain'))
    require(not expected_commit or head == expected_commit, 'Deployment commit mismatch')
    require(not clean or not dirty, 'Deployment requires a clean checkout')
    changes = git('diff', 'HEAD', '--no-ext-diff').encode()
    for name in git('ls-files', '--others', '--exclude-standard', '-z').split('\0'):
        if name:
            changes += name.encode() + (Path(config.BASE_DIR)/name).read_bytes()
    return {'commit': head, 'dirty': dirty, 'checkout_sha256': hashlib.sha256(changes).hexdigest()}


def managed_service():
    plist = Path(config.MCP_LAUNCH_AGENT)
    settings = plistlib.loads(plist.read_bytes())
    expected = [str(Path(config.VENV_PYTHON)), str(Path(config.BASE_DIR)/'babelforge_mcp.py'), '--http']
    require(settings.get('ProgramArguments') == expected and
            settings.get('WorkingDirectory') == config.BASE_DIR and
            settings.get('Label') == plist.stem, 'MCP LaunchAgent checkout/command mismatch')
    return {'target': f'gui/{os.getuid()}/{plist.stem}', 'plist': str(plist),
            'sha256': publication.digest(plist)}


def service_pid(service):
    out = subprocess.check_output(['launchctl', 'print', service['target']], text=True, timeout=15)
    match = re.search(r'^\s*pid = (\d+)\s*$', out, re.M)
    require('state = running' in out and match, 'MCP LaunchAgent is not running')
    return int(match[1])


def restart_service(service, previous_pid):
    subprocess.run(['launchctl', 'kickstart', '-k', service['target']], check=True,
                   capture_output=True, text=True, timeout=30)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            pid = service_pid(service)
            if pid != previous_pid:
                return pid
        except (DeploymentError, subprocess.CalledProcessError):
            pass
        time.sleep(.25)
    raise DeploymentError('MCP did not restart with a new PID')


def ensure_idle():
    records = catalog.reconcile_workers()
    busy = [e['id'] for e in records if catalog._busy(e) or e.get('status') in catalog.WORKER_STATES]
    require(not busy, f'Active or unresolved workers block deployment: {busy}')
    require(not publication._maintenance_marker().exists(), 'Calibre maintenance is unfinished')
    return records


def prepare(manifest_path):
    manifest = json.loads(Path(manifest_path).read_text())
    entries = manifest.get('editions')
    require(isinstance(entries, list) and bool(entries), 'Empty deployment inventory')
    root = path_of(manifest['review_dir'])
    prepared = []
    for entry in entries:
        result, artifact = checked_review(root, entry)
        require(result['quality_report'].get('semantic_audit_version') == SEMANTIC_AUDIT_VERSION,
                'Obsolete semantic review version')
        require(result['quality_report'].get('passed') is True, 'Review has not passed')
        options = publication_options(entry)
        record = catalog.get(options.get('request_id'))
        require(record and record.get('status') == 'published' and
                record.get('publication_stage') == 'completed', 'Edition publication is incomplete')
        identifier = publish.edition_identifier(str(artifact), options['source'],
                                                options['source_id'], entry['language'])
        destination = path_of(options['destination'])
        require(destination.resolve().is_relative_to(Path(config.LIBRARY_DIR).resolve()) and
                not destination.is_symlink(), 'Canonical EPUB is outside the managed library')
        require(record.get('publication_identifier') == identifier and
                record.get('artifact_sha256') == result['sha256'] and
                path_of(record['epub_path']).resolve() == destination.resolve(), 'Catalog edition/artifact mismatch')
        bid = record.get('calibre_id')
        require(type(bid) is int and bid > 0 and record.get('opds_url') == publish.opds_url_for(bid),
                'Catalog canonical acquisition mismatch')
        require(destination.is_file() and publication.digest(destination) == result['sha256'],
                'Canonical library artifact changed')
        state = json.loads((root/entry['id']/'checkpoint.json').read_text())
        book = deserialize(state['book'])
        pairs = [p for c in book.chapters for p in c.pairs]
        report = validate_epub(str(destination), expected_content=pairs, expected_pairs=len(pairs))
        require(report.get('epubcheck') == 'passed', 'Canonical EPUBCheck did not pass')
        prepared.append({'id': entry['id'], 'request_id': record['id'], 'identifier': identifier,
                         'book_id': bid, 'sha256': result['sha256'], 'path': str(destination),
                         'pairs': pairs, 'validation': report})
    for field in ('id', 'request_id', 'identifier', 'book_id', 'path'):
        require(len({r[field] for r in prepared}) == len(prepared), f'Duplicate deployment {field}')
    records = catalog._load()['entries']
    published = {r['id'] for r in records if r.get('status') == 'published'}
    require(published == {r['request_id'] for r in prepared}, 'Inventory does not cover all published requests')
    identities = {r['identifier'] for r in prepared}
    require(not any(r.get('publication_identifier') in identities and r['id'] not in published and
                    r.get('status') == 'completed' for r in records), 'Duplicate completed catalog edition')
    return root, prepared


def verify_inventory(root, prepared):
    """Check managed records and delivery copies without deleting anything."""
    listing = json.loads(publish._run(publish.db_command(
        'list', '--for-machine', '--fields', 'id,title,identifiers')))
    require(isinstance(listing, list), 'Invalid Calibre listing')
    all_ids = [r['id'] for r in listing]
    require(all(type(i) is int for i in all_ids) and len(set(all_ids)) == len(all_ids),
            'Invalid/duplicate Calibre record IDs')
    managed = [r for r in listing if r.get('identifiers', {}).get('babelforge')]
    require(sorted((r['id'], r['identifiers']['babelforge']) for r in managed) ==
            sorted((r['book_id'], r['identifier']) for r in prepared),
            'Calibre contains missing, obsolete or duplicate managed editions')
    advertised = publication.opds_acquisitions()
    require(set(advertised).issubset(set(all_ids)), 'OPDS advertises deleted Calibre records')
    for row in prepared:
        require(len(advertised.get(row['book_id'], [])) == 1, 'OPDS missing/duplicate canonical edition')

    allowed = {Path(r['path']).resolve(): r['sha256'] for r in prepared}
    bundles = set()
    summary_path = root/'summary.json'
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        expected = {r['id']: r for r in prepared}
        deliveries = summary['editions']
        require(summary.get('completed') is True and len(deliveries) == len(expected) and
                {r['id'] for r in deliveries} == set(expected), 'Delivery inventory mismatch')
        for row in deliveries:
            require(row['sha256'] == expected[row['id']]['sha256'], 'Stale delivery metadata')
            allowed[Path(row['delivery_path']).resolve()] = row['sha256']
        bundle = Path(summary['bundle'])
        require(publication.digest(bundle) == summary['bundle_sha256'], 'Delivery bundle changed')
        with zipfile.ZipFile(bundle) as archive:
            names = archive.namelist()
            require(archive.testzip() is None and len(names) == len(set(names)) == len(prepared),
                    'Invalid/duplicate bundle entries')
            hashes = sorted(hashlib.sha256(archive.read(n)).hexdigest() for n in names)
            require(hashes == sorted(r['sha256'] for r in prepared), 'Bundle contains an obsolete edition')
        bundles.add(bundle.resolve())
    for path, digest in allowed.items():
        require(path.is_file() and publication.digest(path) == digest, 'Missing/stale delivery EPUB')
    library = Path(config.LIBRARY_DIR)
    for path in library.rglob('*'):
        require(not path.is_symlink(), f'Symlink in managed library: {path}')
        if path.suffix.lower() == '.epub':
            require(path.resolve() in allowed, f'Untracked/obsolete/duplicate library EPUB: {path}')
        if path.suffix.lower() == '.zip':
            require(path.resolve() in bundles, f'Untracked/obsolete library bundle: {path}')
    obsolete = root/'old-deliverables.json'
    if obsolete.exists():
        for row in json.loads(obsolete.read_text()):
            path = path_of(row['path'])
            require(not path.exists() and not path.is_symlink(), f'Obsolete deliverable remains: {path}')
    return {'managed_editions': len(managed), 'opds_ids': sorted(r['book_id'] for r in prepared),
            'duplicates': 0, 'obsolete_deliverables': 0}


async def mcp_snapshot(prepared):
    # Import client dependencies only for an actual deployment/check command.
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    def value(reply):
        require(not reply.isError, 'MCP tool failed')
        data = reply.structuredContent
        if data is None:
            data = json.loads(next(c.text for c in reply.content if hasattr(c, 'text')))
        return data.get('result', data) if isinstance(data, dict) else data
    async with streamablehttp_client(f'http://{config.MCP_HOST}:{config.MCP_PORT}/mcp') as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            require({'get_status', 'list_recent', 'translate_book', 'retry_request', 'search_books'} <= names,
                    'MCP tools missing')
            require(isinstance(value(await session.call_tool('list_recent', {'limit': 1})), list),
                    'Invalid MCP request listing')
            for row in prepared:
                record = value(await session.call_tool('get_status', {'req_id': row['request_id']}))
                require(record.get('status') == 'published' and record.get('publication_stage') == 'completed' and
                        record.get('artifact_sha256') == row['sha256'] and
                        record.get('publication_identifier') == row['identifier'] and
                        record.get('calibre_id') == row['book_id'], 'MCP returned a stale edition')
    return {'tools': sorted(names), 'editions': len(prepared)}


def check_mcp(prepared):
    import httpx
    def transient(exc):
        if isinstance(exc, BaseExceptionGroup):
            return all(transient(e) for e in exc.exceptions)
        return isinstance(exc, (OSError, httpx.TransportError, TimeoutError))
    deadline = time.monotonic() + 30
    while True:
        try:
            remaining = max(.01, deadline-time.monotonic())
            return asyncio.run(asyncio.wait_for(mcp_snapshot(prepared), timeout=remaining))
        except Exception as exc:
            if not transient(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(.25)


def run(manifest_path, *, check_only=False, expected_commit=None, report_path=None):
    report_path = Path(report_path) if report_path else Path(config.DATA_DIR)/'deployments'/f'{uuid.uuid4().hex}.json'
    # Never overwrite a previous success or an operational/source file.
    require(not report_path.exists(), 'Deployment report already exists; use a fresh path')
    report = {'completed': False, 'mode': 'check' if check_only else 'deploy',
              'started_at': datetime.now(timezone.utc).isoformat(), 'stage': 'initializing',
              'manifest': str(Path(manifest_path).resolve())}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    def checkpoint(stage):
        report['stage'] = stage
        save(report_path, report)
    try:
        with ExitStack() as locks:
            locks.enter_context(file_lock(str(Path(config.DATA_DIR)/'.deployment.lock'), blocking=False))
            locks.enter_context(file_lock(str(Path(config.DATA_DIR)/'.publication.lock'), blocking=False))
            report['manifest_sha256'] = publication.digest(manifest_path)
            report['code'] = git_state(expected_commit, clean=not check_only)
            ensure_idle()
            checkpoint('local_validation')
            root, prepared = prepare(manifest_path)
            report['inventory'] = verify_inventory(root, prepared)
            service = managed_service()
            old_pid = service_pid(service)
            report['mcp_before'] = check_mcp(prepared)
            report['previous_pid'] = old_pid
            if not check_only:
                checkpoint('restarting')
                report['pid'] = restart_service(service, old_pid)
            else:
                report['pid'] = old_pid
            checkpoint('public_validation')
            report['mcp_after'] = check_mcp(prepared)
            with tempfile.TemporaryDirectory(prefix='babelforge-deploy-') as temporary:
                editions = []
                for row in prepared:
                    directory = Path(temporary)/str(row['book_id']); directory.mkdir()
                    public = publication.verify_public(row['book_id'], row['pairs'], directory)
                    editions.append({k: v for k, v in row.items() if k != 'pairs'} | {'public': public})
            # Detect edits/concurrent catalog changes during long downloads.
            ensure_idle()
            _, current = prepare(manifest_path)
            require([{k: r[k] for k in ('id', 'sha256', 'path', 'book_id', 'identifier')} for r in current] ==
                    [{k: r[k] for k in ('id', 'sha256', 'path', 'book_id', 'identifier')} for r in prepared],
                    'Edition changed during deployment')
            report['inventory'] = verify_inventory(root, current)
            require(managed_service() == service, 'LaunchAgent configuration changed during deployment')
            report['mcp_final'] = check_mcp(current)
            require(service_pid(service) == report['pid'], 'MCP restarted unexpectedly during validation')
            require(git_state(expected_commit, clean=not check_only) == report['code'], 'Code changed during deployment')
            require(publication.digest(manifest_path) == report['manifest_sha256'],
                    'Inventory manifest changed during deployment')
            report.update(editions=editions, approved_pairs=sum(len(r['pairs']) for r in current),
                          semantic_version=SEMANTIC_AUDIT_VERSION, completed=True, stage='completed')
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        save(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=path_of)
    parser.add_argument('--check-only', action='store_true', help='Validate without restarting MCP')
    parser.add_argument('--expected-commit', help='Require this full Git commit SHA')
    parser.add_argument('--report', type=path_of, help='New JSON report path; existing files are never overwritten')
    args = parser.parse_args()
    try:
        report = run(args.manifest, check_only=args.check_only, expected_commit=args.expected_commit,
                     report_path=args.report)
    except Exception as exc:
        parser.exit(1, f'Deployment verification failed: {exc}\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
