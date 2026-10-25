"""Verify one scheduled monitor run from receipts and its uniquely linked report."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

import config
from install_openclaw_monitor import cli
from openclaw_line_drill import job_policy
import openclaw_monitor as monitor
from state_store import file_lock

CAUSES = {
    'settings_changed': '설치 기준 또는 모니터 설정이 변경됨',
    'schedule_changed': '기존 오전 9시 예약·실행 환경·알림 정책이 변경됐거나 확인 불가',
    'history_unavailable': '예약 실행 이력을 조회하거나 완전히 읽지 못함',
    'run_missing': '예정 시각의 정기 실행 완료 기록이 없음',
    'run_ambiguous': '예약 실행 또는 동시 수동 실행 기록이 중복되어 증거 연결 불가',
    'run_failed': '정기 점검이 실패하거나 건너뜀',
    'report_unlinked': '정기 실행과 개별 검증 보고서를 유일하게 연결하지 못함',
    'verification_failed': '현재 승인·EPUB·본문·OPDS 검증 결과가 설치 기준에 맞지 않음',
    'normal_not_silent': '정상 점검의 무알림 정책이 유지되지 않음',
}


def read(path):
    path = Path(path)
    monitor.require(not path.is_symlink(), 'Symlink evidence is not accepted')
    return json.loads(path.read_text())


def digest(path):
    monitor.require(not Path(path).is_symlink(), 'Symlink evidence is not accepted')
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    monitor.require(parsed.tzinfo is not None, 'Timestamp needs timezone')
    return parsed.timestamp()*1000


def history(binary, job_id):
    """Read every page; truncation, repeated cursors or duplicate receipts fail closed."""
    entries = []; offset = 0; seen = set(); deadline = time.monotonic()+240
    for _ in range(100):
        monitor.require(time.monotonic() < deadline, 'Run history time budget exceeded')
        page = cli(binary, 'runs', job_id, '--limit', '100', '--offset', str(offset), '--json')
        monitor.require(isinstance(page.get('entries'), list), 'Invalid run history')
        for entry in page['entries']:
            key = (entry.get('runId'), entry.get('action'), entry.get('ts'))
            monitor.require(key not in seen, 'Repeated run receipt')
            seen.add(key); entries.append(entry)
        if page.get('hasMore') is False:
            return entries
        cursor = page.get('nextOffset')
        monitor.require(type(cursor) is int and cursor > offset, 'Incomplete run history')
        offset = cursor
    raise ValueError('Run history exceeds bounded pagination')


def select_run(entries, plan):
    due = plan['scheduled_ms']; end = due + plan['start_grace_seconds']*1000
    window = [e for e in entries if e.get('jobId') == plan['daily_job_id'] and
              type(e.get('runAtMs')) is int and due <= e['runAtMs'] <= end]
    # A simultaneous manual invocation could otherwise lend its report to the scheduler.
    if any(str(e.get('runId', '')).startswith('manual:') for e in window):
        return None, 'run_ambiguous'
    candidates = [e for e in window if e.get('action') == 'finished' and
                  isinstance(e.get('runId'), str) and e['runId']]
    if not candidates: return None, 'run_missing'
    if len(candidates) != 1: return None, 'run_ambiguous'
    entry = candidates[0]
    if type(entry.get('ts')) is not int or not entry['runAtMs'] <= entry['ts'] <= plan['followup_ms']:
        return None, 'run_missing'
    return entry, None


def linked_record(entry, settings):
    """Only canonical per-run ledger/report paths, never latest.json or drill data."""
    runtime = Path(settings['runtime_dir'])
    monitor.require(not runtime.is_symlink(), 'Symlink runtime')
    matches = []
    for path in runtime.glob('*.json'):
        if not re.fullmatch(r'[0-9a-f]{32}\.json', path.name): continue
        record = read(path)
        report_path = Path(config.DATA_DIR)/'deployments'/f'openclaw-{path.stem}.json'
        monitor.require(record.get('report') == str(report_path), 'Noncanonical report reference')
        report = read(report_path) if report_path.exists() else {}
        started = record.get('started_at') or report.get('started_at')
        finished = record.get('finished_at') or report.get('finished_at')
        if not started or not finished: continue
        a, b = stamp(started), stamp(finished)
        if entry['runAtMs']-2000 <= a <= b <= entry['ts']+2000:
            # Reports must themselves have been generated inside this command invocation.
            if report:
                ra = stamp(report['started_at'])
                rb = stamp(report.get('finished_at') or finished)
                monitor.require(a-2000 <= ra <= rb <= b+2000, 'Stale or unrelated report')
            matches.append((record, report))
    monitor.require(len(matches) == 1, 'No unique monitor ledger/report')
    return matches[0]


def daily_notification(record, report, settings):
    """Only the same proven incident may suppress a follow-up alert."""
    error = record.get('error')
    if not isinstance(error, str) or not error: return None
    fingerprint = monitor.incident(error, record.get('error_stage', report.get('stage', 'monitor')), settings['expected'])
    if record.get('incident_fingerprint', fingerprint) != fingerprint: return None
    state = read(Path(settings['runtime_dir'])/'incident.json')
    if state.get('fingerprint') != fingerprint: return None
    if record.get('notification') in {'sent', 'suppressed'} and state.get('delivery') == 'sent':
        return 'already-notified'
    if record.get('notification') == 'unknown' and state.get('delivery') in {'pending', 'unknown'}:
        return 'ambiguous-receipt-no-replay'
    return None


def baseline_unchanged(plan):
    settings = plan['settings']
    monitor.require(digest(plan['config_path']) == plan['config_sha256'] and
                    read(plan['config_path']) == settings and
                    digest(settings['manifest']) == settings['expected']['manifest_sha256'] and
                    settings['expected']['semantic_version'] == monitor.SEMANTIC_AUDIT_VERSION, 'Changed settings')


def assess(plan):
    settings = plan['settings']; issues = []; result = {}; record = report = None
    # Independent audits: a history/report failure cannot skip checking the schedule.
    for _ in range(2):
        try: baseline_unchanged(plan)
        except Exception: issues.append('settings_changed')
        try:
            policy = job_policy(settings, Path(plan['config_path']), plan['daily_job_id'])
            monitor.require(policy == plan['daily_policy'], 'Changed daily job')
            result['schedule_unchanged'] = result.get('schedule_unchanged', True)
        except Exception:
            issues.append('schedule_changed'); result['schedule_unchanged'] = False
        if _ == 1: break
        try:
            entries = history(settings['openclaw'], plan['daily_job_id'])
            entry, issue = select_run(entries, plan)
            if issue: issues.append(issue)
            else:
                result['run_id'] = entry['runId']; result['run_at_ms'] = entry['runAtMs']
                failed = entry.get('status') != 'ok' or entry.get('completionStatus') != 'succeeded'
                if failed: issues.append('run_failed')
                try:
                    record, report = linked_record(entry, settings)
                    result['report'] = record['report']
                    if Path(record['report']).exists(): result['report_sha256'] = digest(record['report'])
                except Exception: issues.append('report_unlinked')
                if not failed and record is not None:
                    try:
                        monitor.require(record.get('completed') is True and report.get('mode') == 'check', 'Incomplete run')
                        monitor.validate(report, settings['expected'], Path(settings['manifest']))
                        result['editions'] = len(report['editions']); result['approved_pairs'] = report['approved_pairs']
                    except Exception: issues.append('verification_failed')
                    if (record.get('notification') != 'not-requested' or
                            entry.get('diagnostics', {}).get('summary') != 'NO_REPLY' or
                            entry.get('deliveryStatus') != 'not-requested'):
                        issues.append('normal_not_silent')
        except Exception: issues.append('history_unavailable')
    result['issues'] = sorted(set(issues))
    result['completed'] = not result['issues']
    if result['issues'] == ['run_failed'] and record is not None:
        try: result['daily_notification'] = daily_notification(record, report, settings)
        except Exception: pass  # Unreadable state cannot silently suppress a new alert.
    return result


def alert(plan, result, directory):
    attempt_path = directory/'delivery.json'
    if attempt_path.exists():
        try: confirmed = read(attempt_path).get('status') == 'sent'
        except Exception: confirmed = False
        return 'suppressed' if confirmed else 'unknown-no-replay'
    # Persist before any CLI/network operation, also surviving crashes and changed errors.
    monitor.save(attempt_path, {'status': 'pending'})
    def transport(argv, **kwargs):
        argv = list(argv); index = argv.index('--message')+1
        causes = ' / '.join(CAUSES[k] for k in result['issues'])
        argv[index] = 'BabelForge 정기 실행 후속 확인 이상\n확인된 원인: '+causes+'\n'+argv[index]
        return subprocess.run(argv, **kwargs)
    try:
        outcome = monitor.notify(plan['settings'], 'inventory: scheduled follow-up '+','.join(result['issues']),
                                 'monitor', directory/'result.json', directory/'result.json',
                                 directory/'incident.json', transport=transport)
    except Exception: outcome = 'unknown'
    monitor.save(attempt_path, {'status': outcome})
    return outcome


def run(plan_path, *, execute=False):
    plan_path = Path(plan_path).absolute(); plan = read(plan_path)
    monitor.require(plan.get('version') == 1 and type(plan['version']) is int, 'Unsupported follow-up plan')
    monitor.require(all(type(plan.get(k)) is int for k in ('scheduled_ms', 'followup_ms', 'start_grace_seconds')) and
                    0 <= plan['start_grace_seconds'] <= 3600 and plan['followup_ms'] > plan['scheduled_ms'] and
                    isinstance(plan.get('daily_job_id'), str) and bool(plan['daily_job_id']), 'Invalid follow-up bounds')
    if not execute:
        return {'execute': False, 'message_sent': False, 'scheduled_ms': plan['scheduled_ms'],
                'followup_ms': plan['followup_ms'], 'daily_job_id': plan['daily_job_id']}
    if time.time()*1000 < plan['followup_ms']:
        return {'completed': False, 'not_due': True}  # Early/manual tests cannot emit missing-run alarms.
    directory = plan_path.parent
    runtime = Path(plan['settings']['runtime_dir']).resolve()
    monitor.require(not directory.resolve().is_relative_to(runtime) and
                    not runtime.is_relative_to(directory.resolve()), 'Follow-up and production paths overlap')
    with file_lock(str(directory/'followup.lock'), blocking=False):
        result = assess(plan)
        result['checked_at'] = datetime.now().astimezone().isoformat()
        monitor.save(directory/'result.json', result)
        result['notification'] = ('not-requested' if result['completed'] else
                                  result.get('daily_notification') or alert(plan, result, directory))
        monitor.save(directory/'result.json', result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--execute', action='store_true', help='Evaluate the due run and send anomalies only')
    args = parser.parse_args()
    try:
        result = run(args.plan, execute=args.execute)
        print('NO_REPLY' if args.execute else json.dumps(result, ensure_ascii=False))
        return 75 if result.get('not_due') else (0 if result.get('completed', not args.execute) else 1)
    except BlockingIOError: return 75
    except Exception:
        parser.exit(1, 'Follow-up configuration/state failure; inspect local records\n')


if __name__ == '__main__':
    raise SystemExit(main())
