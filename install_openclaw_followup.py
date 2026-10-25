"""Pin and install a read-only, anomaly-only follow-up for one daily scheduled run."""
import argparse
from datetime import date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import config
from install_openclaw_monitor import cli
from openclaw_line_drill import job_policy
import openclaw_monitor as monitor
from openclaw_followup import baseline_unchanged
from state_store import file_lock


def prepare(config_path, daily_job_id, day, *, delay_minutes=30):
    config_path = Path(config_path).resolve(); settings = monitor.read_settings(config_path)
    monitor.require(type(delay_minutes) is int and delay_minutes*60 >= settings['check_timeout']+600,
                    'Follow-up must allow the scheduler timeout and start grace')
    scheduled = datetime.combine(date.fromisoformat(day), time(9), ZoneInfo('Asia/Seoul'))
    followup = scheduled + timedelta(minutes=delay_minutes)
    monitor.require(scheduled > datetime.now(ZoneInfo('Asia/Seoul')), 'Target run must be in the future')
    policy = job_policy(settings, config_path, daily_job_id)
    monitor.require(settings['expected']['semantic_version'] == monitor.SEMANTIC_AUDIT_VERSION and
                    hashlib.sha256(Path(settings['manifest']).read_bytes()).hexdigest() ==
                    settings['expected']['manifest_sha256'], 'Changed installation baseline')
    identity = hashlib.sha256((daily_job_id+'|'+day).encode()).hexdigest()[:16]
    return {'version': 1, 'config_path': str(config_path),
            'config_sha256': hashlib.sha256(config_path.read_bytes()).hexdigest(),
            # Private immutable snapshot: keeps alerts on the approved route if config later drifts.
            'settings': settings, 'daily_policy': policy, 'daily_job_id': daily_job_id,
            'scheduled_ms': int(scheduled.timestamp()*1000), 'followup_ms': int(followup.timestamp()*1000),
            'start_grace_seconds': 300, 'day': day, 'identity': identity}


def install(plan, *, existing_job_id=None):
    settings = plan['settings']; binary = settings['openclaw']
    baseline_unchanged(plan)
    monitor.require(job_policy(settings, Path(plan['config_path']), plan['daily_job_id']) == plan['daily_policy'],
                    'Daily job changed before installation')
    directory = Path(config.DATA_DIR)/'openclaw-monitor/followups'/plan['identity']
    runtime = Path(settings['runtime_dir']).resolve()
    monitor.require(not directory.resolve().is_relative_to(runtime) and
                    not runtime.is_relative_to(directory.resolve()), 'Follow-up paths overlap production')
    name = 'babelforge-followup-'+plan['identity']; declaration = name+'-v1'
    legacy = 'babelforge-mcp-opds-followup-'+plan['day']
    at = datetime.fromtimestamp(plan['followup_ms']/1000, ZoneInfo('UTC')).isoformat()
    with file_lock(str(directory/'installation.lock'), blocking=False):
        response = cli(binary, 'list', '--all', '--json'); jobs = response.get('jobs', [])
        matches = [j for j in jobs if j.get('declarationKey') == declaration or j.get('name') == name or
                   j.get('declarationKey') == legacy+'-v1' or j.get('name') == legacy]
        monitor.require(len(matches) <= 1, 'Duplicate follow-ups; resolve explicitly')
        if existing_job_id:
            monitor.require(len(matches) == 1 and matches[0]['id'] == existing_job_id, 'Unrelated follow-up ID')
        if matches:
            job = matches[0]
            monitor.require(job.get('declarationKey') == declaration or
                            (existing_job_id == job['id'] and job.get('declarationKey') == legacy+'-v1'),
                            'Legacy follow-up requires its explicit ID; foreign declarations are never adopted')
            monitor.require(not job.get('state', {}).get('runningAtMs'), 'Follow-up is active')
        path = directory/'plan.json'
        if path.exists():
            monitor.require(json.loads(path.read_text()) == plan, 'Existing follow-up plan changed; do not rebase silently')
        else: monitor.save(path, plan)
        argv = [config.VENV_PYTHON, str(Path(config.BASE_DIR)/'openclaw_followup.py'),
                '--plan', str(path.resolve()), '--execute']
        flags = ['--at', at, '--tz', 'Asia/Seoul', '--agent', 'main', '--session', 'isolated',
                 '--command-argv', json.dumps(argv), '--command-cwd', config.BASE_DIR,
                 '--command-env', 'JAVA_HOME='+settings['java_home'],
                 '--command-env', 'PATH='+settings['execution_path'], '--timeout-seconds', '600',
                 '--no-output-timeout-seconds', '600', '--keep-after-run', '--no-deliver', '--json']
        if matches:
            identifier = matches[0]['id']  # retain a deliberately adopted legacy declaration too
        else:
            response = cli(binary, 'add', '--name', name, '--declaration-key', declaration, *flags, '--disabled')
            identifier = response.get('job', response)['id']
            monitor.save(directory/'created.json', {'job_id': identifier})
        try:
            cli(binary, 'edit', identifier, *flags, '--no-failure-alert', '--enable')
            response = cli(binary, 'get', identifier, '--json'); current = response.get('job', response)
            monitor.require(current.get('enabled') is True and current.get('deleteAfterRun') is False and
                            current.get('delivery', {}).get('mode') == 'none' and current.get('failureAlert') is False and
                            current.get('schedule', {}).get('kind') == 'at' and
                            datetime.fromisoformat(current['schedule']['at'].replace('Z', '+00:00')).timestamp()*1000 ==
                            plan['followup_ms'] and current.get('payload', {}).get('kind') == 'command' and
                            current['payload'].get('argv') == argv and current['payload'].get('cwd') == config.BASE_DIR and
                            current['payload'].get('timeoutSeconds') == current['payload'].get('noOutputTimeoutSeconds') == 600 and
                            current['payload'].get('env') == {'JAVA_HOME': settings['java_home'], 'PATH': settings['execution_path']},
                            'Follow-up policy not retained')
            monitor.require(job_policy(settings, Path(plan['config_path']), plan['daily_job_id']) == plan['daily_policy'],
                            'Daily job changed during installation')
            baseline_unchanged(plan)
        except Exception:
            disabled = False
            try:
                cli(binary, 'edit', identifier, '--disable', '--no-deliver', '--no-failure-alert', '--json')
                disabled = True
            except Exception: pass
            monitor.save(directory/'installation-failure.json', {'job_id': identifier, 'disable_requested': disabled})
            raise
        receipt = {'job_id': identifier, 'plan': str(path), 'at': at, 'daily_job_unchanged': True,
                   'notification': 'anomalies only; normal and recovery silent'}
        monitor.save(directory/'installation.json', receipt)
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=monitor.DEFAULT_CONFIG)
    parser.add_argument('--job-id', required=True, help='Existing daily monitor ID')
    parser.add_argument('--date', required=True, help='Target daily run date, YYYY-MM-DD in Asia/Seoul')
    parser.add_argument('--delay-minutes', type=int, default=30)
    parser.add_argument('--existing-job-id', help='Explicitly adopt the legacy one-shot follow-up ID')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        plan = prepare(args.config, args.job_id, args.date, delay_minutes=args.delay_minutes)
        result = install(plan, existing_job_id=args.existing_job_id) if args.apply else {
            'apply': False, 'daily_job_id': plan['daily_job_id'], 'date': plan['day'],
            'scheduled_ms': plan['scheduled_ms'], 'followup_ms': plan['followup_ms'],
            'editions': len(plan['settings']['expected']['editions']), 'notification': 'anomalies only'}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        parser.exit(1, f'Follow-up installation failed: {type(exc).__name__}\n')


if __name__ == '__main__':
    main()
