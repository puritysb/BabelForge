"""Preview or explicitly send one isolated LINE anomaly drill, then verify recovery."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

import config
from install_openclaw_monitor import cli, DECLARATION, JOB_NAME
import openclaw_monitor as monitor
from state_store import file_lock


def job_policy(settings, settings_path, job_id, *, reserve=False):
    response = cli(settings['openclaw'], 'list', '--all', '--json')
    jobs = response.get('jobs', []) if isinstance(response, dict) else response
    matches = [j for j in jobs if j.get('declarationKey') == DECLARATION or j.get('name') == JOB_NAME]
    monitor.require(len(matches) == 1 and matches[0].get('id') == job_id,
                    'Missing, duplicate or unrelated monitor job')
    job = matches[0]
    monitor.require(job.get('declarationKey') == DECLARATION and job.get('enabled') is True,
                    'Monitor declaration is not enabled')
    monitor.require(not job.get('state', {}).get('runningAtMs'), 'Scheduled monitor is active')
    monitor.require(job.get('schedule') == {'kind': 'cron', 'expr': '0 9 * * *',
                                           'tz': 'Asia/Seoul', 'staggerMs': 0}, 'Unexpected daily schedule')
    monitor.require(job.get('delivery', {}).get('mode') == 'none' and job.get('failureAlert') is False,
                    'Native notifications must be disabled')
    payload = job.get('payload', {})
    monitor.require(payload.get('kind') == 'command' and payload.get('argv') == [
        config.VENV_PYTHON, str(Path(config.BASE_DIR)/'openclaw_monitor.py'),
        '--config', str(settings_path.resolve())] and payload.get('cwd') == config.BASE_DIR,
        'Scheduled command does not match these settings')
    monitor.require(payload.get('env') == {'JAVA_HOME': settings['java_home'],
                                         'PATH': settings['execution_path']} and
                    payload.get('timeoutSeconds') == settings['check_timeout'] + 300 and
                    payload.get('noOutputTimeoutSeconds') == settings['check_timeout'] + 300,
                    'Scheduled environment or time budget differs')
    if reserve:
        next_run = job.get('nextRunAtMs', job.get('state', {}).get('nextRunAtMs'))
        monitor.require(type(next_run) is int and
                        next_run/1000 > time.time() + settings['check_timeout'] + 600,
                        'Too close to the scheduled run; try after the daily check')
    return {k: job.get(k) for k in ('id', 'declarationKey', 'name', 'enabled', 'schedule',
                                   'payload', 'delivery', 'failureAlert', 'agentId', 'sessionTarget', 'wakeMode')}


def protected_files(settings_path, settings):
    """Fingerprints only; never copy credentials or restore over someone else's edits."""
    runtime = Path(settings['runtime_dir'])
    monitor.require(not runtime.is_symlink(), 'Symlink in protected monitor state')
    paths = {settings_path, settings_path.parent/'installation.json', Path(settings['manifest'])}
    if runtime.exists():
        for path in runtime.rglob('*'):
            monitor.require(not path.is_symlink(), 'Symlink in protected monitor state')
            if not path.name.endswith('.lock') and not path.is_dir():
                paths.add(path)
    result = {}
    for path in sorted(paths):
        monitor.require(not path.is_symlink(), 'Symlink in protected monitor state')
        result[str(path)] = ({'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                              'mode': path.stat().st_mode & 0o777} if path.exists() else None)
    return result


def run_drill(settings_path, job_id, *, live=False):
    settings_path = Path(settings_path).absolute()
    settings = monitor.read_settings(settings_path)
    if not live:
        return {'live': False, 'message_sent': False, 'job_id': job_id,
                'plan': ['isolated first anomaly (one labelled LINE message)',
                         'identical anomaly (suppressed)', 'real check-only recovery (silent)',
                         'clear isolated incident; compare production state and 09:00 schedule']}
    root = Path(config.DATA_DIR)/'openclaw-monitor/drills'
    runtime = Path(settings['runtime_dir']).resolve()
    monitor.require(not root.resolve().is_relative_to(runtime) and
                    not runtime.is_relative_to(root.resolve()), 'Drill and production paths overlap')
    # Prevent a second drill or scheduler from changing production incident state.
    with file_lock(str(runtime/'monitor.lock'), blocking=False):
        before_job = job_policy(settings, settings_path, job_id, reserve=True)
        before_files = protected_files(settings_path, settings)
        directory = root/uuid.uuid4().hex
        directory.mkdir(parents=True, mode=0o700)  # fresh non-replayable workspace
        isolated = copy.deepcopy(settings)
        isolated['runtime_dir'] = str(directory/'runtime')
        summary = {'completed': False, 'live': True, 'job_id': job_id, 'phases': [],
                   'send_attempts': [], 'started_at': datetime.now().astimezone().isoformat()}
        result_path = directory/'result.json'
        phase = 'first_anomaly'
        monitor.save(directory/'protected-before.json', before_files)
        monitor.save(result_path, summary)

        def transport(argv, **kwargs):
            # Cap attempts durably BEFORE sending, including timeout/unknown receipts.
            monitor.require(phase == 'first_anomaly' and not summary['send_attempts'],
                            'Blocked additional drill message')
            attempt = {'receipt': 'unknown'}
            summary['send_attempts'].append(attempt)
            monitor.save(result_path, summary)
            reply = subprocess.run(argv, **kwargs)
            try:
                if reply.returncode == 0 and monitor.receipt(json.loads(reply.stdout)):
                    attempt['receipt'] = 'confirmed'
            except ValueError:
                pass
            monitor.save(result_path, summary)
            return reply

        def notifier(*args):
            if phase == 'healthy_recovery':
                return 'recovery-failure-not-sent'  # never turn a failed recovery into another push
            return monitor.notify(*args, drill=True, transport=transport)

        def injected_failure(_settings, report, log):
            monitor.save(report, {'completed': False, 'mode': 'check', 'stage': 'initializing',
                                 'error': 'Controlled LINE drill: inventory mismatch; production unchanged'})
            monitor.save(log, {'test_only': True, 'production_checker_executed': False})
            return 1

        try:
            for phase, expected in [('first_anomaly', 'sent'), ('same_anomaly', 'suppressed'),
                                    ('healthy_recovery', 'not-requested')]:
                code = monitor.run(isolated, checker=(None if phase == 'healthy_recovery' else injected_failure),
                                   notifier=notifier, report_dir=directory)
                record = json.loads((directory/'runtime/latest.json').read_text())
                summary['phases'].append({'phase': phase, 'exit_code': code, 'record': record})
                monitor.save(result_path, summary)
                monitor.require(record.get('notification') == expected and
                                code == (0 if phase == 'healthy_recovery' else 1), 'Unexpected drill phase result')
                monitor.require(len(summary['send_attempts']) == 1 and
                                summary['send_attempts'][0]['receipt'] == 'confirmed',
                                'LINE receipt unconfirmed; do not automatically replay')
            monitor.require(json.loads((directory/'runtime/incident.json').read_text()) == {'fingerprint': None},
                            'Recovery did not clear the isolated incident')
            summary['phases_passed'] = True
        except Exception as exc:
            # Raw CLI errors/recipient IDs must not escape into the summary or console.
            summary['failure_type'] = type(exc).__name__
            summary['failed_phase'] = phase
        finally:
            # Preserve forensic evidence separately, then disarm only our isolated incident.
            incident = directory/'runtime/incident.json'
            try:
                if incident.exists():
                    monitor.save(directory/'incident-before-cleanup.json', json.loads(incident.read_text()))
            except Exception as exc:
                summary['cleanup_failure_type'] = type(exc).__name__
            try:
                monitor.save(incident, {'fingerprint': None})
                summary['isolated_incident_cleared'] = True
            except Exception as exc:
                summary['cleanup_failure_type'] = type(exc).__name__
            try:
                after_files = protected_files(settings_path, settings)
                monitor.save(directory/'protected-after.json', after_files)
                summary['production_state_unchanged'] = after_files == before_files
            except Exception as exc:
                summary['state_verification_failure_type'] = type(exc).__name__
            try:
                after_job = job_policy(settings, settings_path, job_id)
                summary['schedule_and_job_unchanged'] = after_job == before_job
                summary['schedule'] = after_job['schedule']
            except Exception as exc:
                summary['verification_failure_type'] = type(exc).__name__
            summary['completed'] = all(summary.get(k) is True for k in (
                'phases_passed', 'isolated_incident_cleared', 'production_state_unchanged', 'schedule_and_job_unchanged')) and \
                'cleanup_failure_type' not in summary
            summary['finished_at'] = datetime.now().astimezone().isoformat()
            monitor.save(result_path, summary)
        return {'completed': summary['completed'], 'report': str(result_path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=monitor.DEFAULT_CONFIG)
    parser.add_argument('--job-id', required=True, help='Existing installed monitor ID')
    parser.add_argument('--send-test-alert', action='store_true', help='Explicitly authorize one real labelled LINE drill')
    args = parser.parse_args()
    try:
        result = run_drill(args.config, args.job_id, live=args.send_test_alert)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('completed', not args.send_test_alert) else 1
    except Exception as exc:
        parser.exit(1, f'LINE drill refused: {type(exc).__name__}\n')


if __name__ == '__main__':
    raise SystemExit(main())
