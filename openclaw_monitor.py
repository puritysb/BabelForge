"""Repository-owned read-only monitor; normal/recovery runs never send messages."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import uuid
from zoneinfo import ZoneInfo

import config
from java_runtime import java_environment
from quality_audit import SEMANTIC_AUDIT_VERSION
from state_store import file_lock, _write

DEFAULT_CONFIG = Path(config.DATA_DIR)/'openclaw-monitor/config.json'


def save(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    _write(str(path), value)  # unique temporary file, fsync, mode 0600


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_settings(path):
    settings = json.loads(Path(path).read_text())
    require(type(settings.get('version')) is int and settings['version'] == 1, 'Unsupported monitor settings')
    require(isinstance(settings.get('expected'), dict) and bool(settings['expected'].get('editions')),
            'Missing expected inventory')
    require(all(isinstance(settings.get(k), str) and settings[k].strip()
                for k in ('line_target', 'openclaw', 'java_home', 'manifest', 'runtime_dir', 'timezone')),
            'Missing approved notification route or runtime settings')
    require(type(settings.get('check_timeout')) is int and settings['check_timeout'] > 0,
            'Invalid check timeout')
    return settings


def baseline(report, manifest):
    """Pin installation expectations to a completed, fully checked report."""
    require(report.get('completed') is True and report.get('stage') == 'completed', 'Incomplete baseline report')
    require(report.get('semantic_version') == SEMANTIC_AUDIT_VERSION, 'Obsolete baseline review version')
    require(Path(report['manifest']).resolve() == manifest.resolve(), 'Baseline manifest path differs')
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    require(report.get('manifest_sha256') == digest, 'Baseline manifest changed')
    expected = {'manifest_sha256': digest, 'semantic_version': report['semantic_version'],
                'approved_pairs': report['approved_pairs'], 'editions': [
                    {k: row[k] for k in ('id', 'book_id', 'sha256')} for row in report['editions']]}
    validate(report, expected, manifest)
    return expected


def validate(report, expected, manifest):
    require(report.get('completed') is True and report.get('stage') == 'completed', 'Incomplete verification')
    require(report.get('mode') in {'check', 'deploy'}, 'Unknown verification mode')
    require(type(report.get('approved_pairs')) is int and report['approved_pairs'] > 0,
            'Invalid approved paragraph count')
    require(Path(report.get('manifest', '')).resolve() == manifest.resolve(), 'Wrong manifest path')
    for key in ('manifest_sha256', 'semantic_version', 'approved_pairs'):
        require(report.get(key) == expected[key], f'Changed verification baseline: {key}')
    rows = report.get('editions') or []
    wanted = expected['editions']
    require(isinstance(rows, list) and isinstance(wanted, list), 'Invalid edition arrays')
    require(len(rows) == len(wanted) > 0, 'Missing/extra editions')
    require(all(isinstance(r, dict) and isinstance(r.get('id'), str) and r['id'] and
                type(r.get('book_id')) is int and r['book_id'] > 0 and
                isinstance(r.get('sha256'), str) and r['sha256'] for r in rows + wanted),
            'Invalid edition identity types')
    for key in ('id', 'book_id'):
        require(len({r[key] for r in wanted}) == len(wanted), 'Duplicate expected edition')
    require(sorted((r['id'], r['book_id'], r['sha256']) for r in rows) ==
            sorted((r['id'], r['book_id'], r['sha256']) for r in wanted), 'Changed edition/artifact inventory')
    inventory = report.get('inventory') or {}
    require(type(inventory.get('managed_editions')) is int and
            all(type(inventory.get(k)) is int for k in ('duplicates', 'obsolete_deliverables')) and
            isinstance(inventory.get('opds_ids'), list) and
            all(type(v) is int for v in inventory['opds_ids']) and
            inventory.get('managed_editions') == len(wanted) and
            inventory.get('opds_ids') == sorted(r['book_id'] for r in wanted) and
            inventory.get('duplicates') == inventory.get('obsolete_deliverables') == 0, 'Stale/duplicate inventory')
    total = 0
    for row in rows:
        local = row.get('validation') or {}
        require(type(local.get('pairs')) is int and local['pairs'] > 0, 'Missing paragraph count')
        total += local['pairs']
        for check in (local, (row.get('public') or {}).get('validation') or {},
                      (row.get('public') or {}).get('stable_validation') or {}):
            require(check.get('passed') is True and check.get('epubcheck') == 'passed' and
                    check.get('content_compared') is True and check.get('pairs') == local['pairs'],
                    'EPUB standard/content comparison incomplete')
    require(total == expected['approved_pairs'], 'Paragraph inventory mismatch')
    for key in ('mcp_before', 'mcp_after', 'mcp_final'):
        count = (report.get(key) or {}).get('editions')
        require(type(count) is int and count == len(wanted), 'MCP inventory incomplete')
    require(report.get('finished_at'), 'Unfinished verification report')


def check_command(settings, report_path, log_path):
    """Timeout kills the checker process group, including Java descendants."""
    env = java_environment(settings['java_home'])
    env['PATH'] = settings.get('execution_path', env['PATH'])
    argv = [config.VENV_PYTHON, str(Path(config.BASE_DIR)/'deploy_mcp.py'),
            str(Path(settings['manifest'])), '--check-only', '--report', str(report_path)]
    with log_path.open('w', encoding='utf-8') as log:
        os.chmod(log_path, 0o600)
        process = subprocess.Popen(argv, cwd=config.BASE_DIR, env=env, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return process.wait(timeout=settings['check_timeout'])
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            raise TimeoutError('Monitor checker exceeded its time budget')


def reason(error):
    """Only predefined facts go to LINE; book text and raw diagnostics stay local."""
    text = str(error).lower()
    for markers, code, description in (
        (('java', 'bad cpu type'), 'java', 'Java 17 실행 환경 오류'),
        (('blockingioerror', 'lock', 'active or unresolved workers', 'maintenance is unfinished'),
         'busy', '배포·번역 작업과 경합하여 검사하지 못함'),
        (('manifest', 'baseline', 'inventory'), 'inventory', '검사 기준 또는 최신본 목록 불일치'),
        (('approval', 'profile', 'source changed'), 'approval', '현재 승인·원문·프로필 검증 실패'),
        (('epub', 'content', 'paragraph'), 'epub', 'EPUB 표준 또는 승인된 본문 대조 실패'),
        (('timeout', 'timed out', 'time budget'), 'timeout', '점검 시간 제한 초과'),
        (('connection', 'urlerror', 'http', 'transport'), 'connection', 'MCP·OPDS 접속 실패'),
    ):
        if any(marker in text for marker in markers):
            return code, description
    return 'unknown', '원인 미확인: 로컬 보고서와 로그 확인 필요'


def incident(error, stage, expected):
    # Remove per-run IDs and temporary download directories from the identity.
    text = re.sub(r'\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b[0-9a-f]{32}\b', '<run>', str(error), flags=re.I)
    text = re.sub(r'babelforge-(?:deploy|publish)-[^/\s]+', 'babelforge-tmp', text)
    value = {'reason': reason(error)[0], 'stage': stage, 'ids': sorted(r['id'] for r in expected['editions']),
             'detail': text}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def receipt(value):
    if isinstance(value, dict):
        if value.get('ok') is False:
            return False
        if (isinstance(value.get('messageId'), str) and value['messageId'].strip() or
                isinstance(value.get('messageIds'), list) and value['messageIds'] and
                all(isinstance(v, str) and v.strip() for v in value['messageIds'])):
            return True
        return any(receipt(v) for v in value.values())
    return isinstance(value, list) and any(receipt(v) for v in value)


def notify(settings, error, stage, report_path, log_path, state_path):
    fingerprint = incident(error, stage, settings['expected'])
    try:
        previous = json.loads(state_path.read_text())
        if not isinstance(previous, dict): previous = {}
    except (OSError, ValueError):
        previous = {}  # corrupt notification cache must not suppress the first incident
    if previous.get('fingerprint') == fingerprint:
        return 'suppressed' if previous.get('delivery') == 'sent' else 'unknown'
    now = datetime.now(ZoneInfo(settings['timezone'])).isoformat()
    names = ', '.join(r['id'] for r in settings['expected']['editions'])
    # Names/phase are identifiers from local settings, never source passages.
    names = re.sub(r'[^\w ,.-]', '?', names)[:400]
    phase = stage if stage in {'initializing', 'local_validation', 'restarting', 'public_validation', 'completed'} else 'monitor'
    message = (f'BabelForge 점검 이상 {now}\n영향: {names}\n단계: {phase}\n'
               f'원인: {reason(error)[1]}\n보고서: {report_path}\n로그: {log_path}\n'
               '조치: 로컬 기록을 확인해 원인 수정 후 재점검. 자동 수리는 하지 않음.')
    # Persist before sending. A crash or ambiguous CLI result cannot cause a duplicate push.
    state = {'fingerprint': fingerprint, 'delivery': 'pending', 'report': str(report_path), 'time': now}
    save(state_path, state)
    try:
        env = os.environ.copy()
        env['PATH'] = settings.get('execution_path', env.get('PATH', os.defpath))
        result = subprocess.run([settings['openclaw'], 'message', 'send', '--channel', 'line',
                                 '--target', settings['line_target'], '--message', message, '--json'],
                                capture_output=True, text=True, timeout=45, env=env)
        confirmed = result.returncode == 0 and receipt(json.loads(result.stdout))
    except (OSError, ValueError, subprocess.SubprocessError):
        confirmed = False
    state['delivery'] = 'sent' if confirmed else 'unknown'
    save(state_path, state)
    return state['delivery']


def run(settings):
    runtime = Path(settings['runtime_dir']); runtime.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    report_path = Path(config.DATA_DIR)/'deployments'/f'openclaw-{run_id}.json'
    log_path = runtime/f'{run_id}.log'
    state_path = runtime/'incident.json'
    with file_lock(str(runtime/'monitor.lock'), blocking=False):
        record = {'report': str(report_path), 'log': str(log_path), 'completed': False}
        report = {}
        error = None
        try:
            result = check_command(settings, report_path, log_path)
            if report_path.exists(): report = json.loads(report_path.read_text())
            require(result == 0, report.get('error') or 'Verification command failed')
            require(report.get('mode') == 'check', 'Monitor must use check-only')
            validate(report, settings['expected'], Path(settings['manifest']))
            save(state_path, {'fingerprint': None})  # recovery is silent; recurrence is a new incident
            record['completed'] = True
            record['notification'] = 'not-requested'
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            record['error'] = error  # local record only
            record['notification'] = notify(settings, error, report.get('stage', 'monitor'),
                                             report_path, log_path, state_path)
        save(runtime/f'{run_id}.json', record)
        save(runtime/'latest.json', record)
        print('NO_REPLY' if not error else f'Monitor failed; report={report_path}; log={log_path}')
        return 0 if not error else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    try:
        return run(read_settings(args.config))
    except BlockingIOError:
        print('Monitor already running; skipped overlapping invocation', file=sys.stderr)
        return 75
    except Exception:
        print('Monitor configuration/state failure; inspect local settings', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
