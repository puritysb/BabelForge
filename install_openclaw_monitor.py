"""Prepare or reconcile an OpenClaw command job against repository-owned code."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

import config
from java_runtime import java_environment
from openclaw_monitor import DEFAULT_CONFIG, baseline, require, save

JOB_NAME = 'babelforge-mcp-opds-daily'
DECLARATION = 'babelforge-mcp-opds-daily-v1'


def cli(binary, *args):
    return json.loads(subprocess.check_output([binary, 'cron', *args], text=True, timeout=60))


def prepare(manifest, report_path, target, *, java_home=None, openclaw=None, timeout=1200):
    manifest = Path(manifest).resolve()
    require(manifest.is_relative_to(Path(config.BASE_DIR).resolve()), 'Keep the review manifest inside this checkout')
    require(isinstance(target, str) and bool(target.strip()), 'An explicitly approved LINE target is required')
    require(type(timeout) is int and timeout > 0, 'Invalid monitor timeout')
    binary = openclaw or shutil.which('openclaw') or str(Path.home()/'.openclaw/bin/openclaw')
    require(Path(binary).is_file(), 'OpenClaw CLI not found; specify --openclaw')
    report = json.loads(Path(report_path).read_text())
    expected = baseline(report, manifest)
    env = java_environment(java_home)
    # Preserve the CLI entry point and the installer-resolved Node/Bun search
    # path too; npm CLIs may use /usr/bin/env node under a sparse launchd PATH.
    execution_path = os.pathsep.join(dict.fromkeys([
        str(Path(env['JAVA_HOME'])/'bin'), str(Path(binary).absolute().parent),
        *env['PATH'].split(os.pathsep)]))
    return {'version': 1, 'manifest': str(manifest), 'expected': expected,
            'java_home': env['JAVA_HOME'], 'openclaw': str(Path(binary).absolute()),
            'execution_path': execution_path,
            'line_target': target.strip(), 'timezone': 'Asia/Seoul', 'check_timeout': timeout,
            'runtime_dir': str(Path(config.DATA_DIR)/'openclaw-monitor/runtime')}


def reconcile(settings, settings_path, *, job_id=None):
    """Idempotent declaration; refuse ambiguity and never modify another job."""
    binary = settings['openclaw']
    response = cli(binary, 'list', '--all', '--json')
    jobs = response.get('jobs', []) if isinstance(response, dict) else response
    matches = [j for j in jobs if j.get('declarationKey') == DECLARATION or j.get('name') == JOB_NAME]
    require(len(matches) <= 1, 'Duplicate monitor jobs; resolve before reinstalling')
    if job_id:
        require(len(matches) == 1 and matches[0]['id'] == job_id, 'Requested job is not this monitor')
    if matches:
        declaration = matches[0].get('declarationKey')
        require(declaration == DECLARATION or (not declaration and job_id == matches[0]['id']),
                'A matching name alone cannot identify this monitor; confirm a legacy job with --job-id')
        require(not matches[0].get('state', {}).get('runningAtMs'), 'Monitor is running; wait before reinstalling')
    argv = [config.VENV_PYTHON, str(Path(config.BASE_DIR)/'openclaw_monitor.py'),
            '--config', str(settings_path.resolve())]
    outer_timeout = settings['check_timeout'] + 300  # checker shutdown + alert receipt + durable logs
    flags = ['--name', JOB_NAME, '--description', 'Repository-managed BabelForge check; only anomalies notify LINE',
             '--cron', '0 9 * * *', '--tz', settings['timezone'], '--exact', '--agent', 'main',
             '--session', 'isolated', '--command-argv', json.dumps(argv), '--command-cwd', config.BASE_DIR,
             '--command-env', 'JAVA_HOME='+settings['java_home'],
             '--command-env', 'PATH='+settings.get('execution_path', settings['java_home']+'/bin:'+os.defpath),
             '--timeout-seconds', str(outer_timeout), '--no-output-timeout-seconds', str(outer_timeout),
             '--output-max-bytes', '16384', '--no-deliver', '--json']
    save(settings_path, settings)  # never overwrite the incident cache or run history
    if matches:
        identifier = matches[0]['id']
    else:
        response = cli(binary, 'add', *flags, '--declaration-key', DECLARATION, '--disabled')
        identifier = response.get('job', response)['id']
    cli(binary, 'edit', identifier, *flags, '--no-failure-alert', '--enable')
    current = cli(binary, 'get', identifier)
    current = current.get('job', current)
    require(current.get('enabled') is True and current.get('failureAlert') is False and
            current.get('delivery', {}).get('mode') == 'none' and
            current.get('payload', {}).get('argv') == argv and
            current.get('payload', {}).get('timeoutSeconds') == outer_timeout and
            current.get('payload', {}).get('noOutputTimeoutSeconds') == outer_timeout and
            current.get('schedule') == {'kind': 'cron', 'expr': '0 9 * * *',
                                        'tz': settings['timezone'], 'staggerMs': 0},
            'OpenClaw did not retain the requested monitor policy')
    receipt = {'job_id': identifier, 'settings': str(settings_path),
               'schedule': current['schedule'], 'enabled': True, 'notification': 'anomalies only'}
    save(settings_path.parent/'installation.json', receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--baseline-report', type=Path, required=True)
    parser.add_argument('--line-target', required=True, help='Existing personal LINE target approved by the user')
    parser.add_argument('--java-home')
    parser.add_argument('--openclaw')
    parser.add_argument('--check-timeout', type=int, default=1200)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--job-id', help='Require this existing monitor identity during migration')
    parser.add_argument('--apply', action='store_true', help='Write local settings and create/update the job')
    args = parser.parse_args()
    try:
        settings = prepare(args.manifest, args.baseline_report, args.line_target,
                           java_home=args.java_home, openclaw=args.openclaw, timeout=args.check_timeout)
        if args.apply:
            result = reconcile(settings, args.config, job_id=args.job_id)
        else:
            result = {'apply': False, 'settings_path': str(args.config), 'java_home': settings['java_home'],
                      'editions': len(settings['expected']['editions']), 'schedule': '09:00 Asia/Seoul',
                      'notification': 'anomalies only; recipient withheld'}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        parser.exit(1, f'Monitor installation failed: {type(exc).__name__}\n')


if __name__ == '__main__':
    main()
