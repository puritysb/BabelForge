"""Offline drill regressions: no Gateway, LINE, Java, or production files."""
from contextlib import contextmanager
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import config
import openclaw_line_drill as drill
import openclaw_monitor as monitor
from state_store import file_lock


class DrillTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); data = root/'data'; runtime = data/'openclaw-monitor/runtime'
            runtime.mkdir(parents=True)
            manifest = root/'manifest.json'; manifest.write_text('{}')
            check = {'pairs': 3, 'passed': True, 'epubcheck': 'passed', 'content_compared': True}
            report = {'completed': True, 'stage': 'completed', 'mode': 'check', 'finished_at': 'now',
                      'manifest': str(manifest), 'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
                      'semantic_version': monitor.SEMANTIC_AUDIT_VERSION, 'approved_pairs': 3,
                      'inventory': {'managed_editions': 1, 'opds_ids': [7], 'duplicates': 0, 'obsolete_deliverables': 0},
                      'editions': [{'id': 'book', 'book_id': 7, 'sha256': 'approved', 'validation': check,
                                    'public': {'validation': check, 'stable_validation': check}}],
                      **{key: {'editions': 1} for key in ('mcp_before', 'mcp_after', 'mcp_final')}}
            settings = {'version': 1, 'manifest': str(manifest), 'expected': monitor.baseline(report, manifest),
                        'runtime_dir': str(runtime), 'java_home': '/jdk', 'line_target': 'private-owner',
                        'openclaw': '/test/openclaw', 'execution_path': '/jdk/bin:/usr/bin',
                        'timezone': 'Asia/Seoul', 'check_timeout': 1200}
            settings_path = runtime.parent/'config.json'; monitor.save(settings_path, settings)
            monitor.save(runtime/'incident.json', {'fingerprint': 'production-incident', 'delivery': 'unknown'})
            monitor.save(runtime/'latest.json', {'production': True})
            job = {'id': 'existing', 'name': drill.JOB_NAME, 'declarationKey': drill.DECLARATION,
                   'enabled': True, 'schedule': {'kind': 'cron', 'expr': '0 9 * * *', 'tz': 'Asia/Seoul', 'staggerMs': 0},
                   'delivery': {'mode': 'none'}, 'failureAlert': False,
                   'nextRunAtMs': int((time.time()+90000)*1000),
                   'payload': {'kind': 'command', 'argv': [config.VENV_PYTHON, str(root/'openclaw_monitor.py'),
                               '--config', str(settings_path)], 'cwd': str(root),
                               'env': {'JAVA_HOME': '/jdk', 'PATH': settings['execution_path']},
                               'timeoutSeconds': 1500, 'noOutputTimeoutSeconds': 1500}}
            def checker(s, path, log):
                monitor.save(path, report); monitor.save(log, {'real_check_mock': True}); return 0
            with patch.object(config, 'BASE_DIR', str(root)), patch.object(config, 'DATA_DIR', str(data)), \
                    patch.object(drill, 'cli', side_effect=lambda *a: {'jobs': [copy.deepcopy(job)]}) as cli, \
                    patch.object(monitor, 'check_command', side_effect=checker) as recovery, \
                    patch.object(drill.subprocess, 'run', return_value=subprocess.CompletedProcess(
                        [], 0, '{"payload":{"messageId":"accepted"}}', '')) as send, patch('builtins.print'):
                yield locals()

    def execute(self, f):
        result = drill.run_drill(f['settings_path'], 'existing', live=True)
        result['summary'] = json.loads(Path(result['report']).read_text())
        return result

    def test_default_preview_never_calls_gateway_checker_or_sender_or_writes(self):
        with self.fixture() as f:
            before = sorted(str(p) for p in f['root'].rglob('*'))
            result = drill.run_drill(f['settings_path'], 'existing')
            self.assertFalse(result['live']); self.assertFalse(result['message_sent'])
            f['cli'].assert_not_called(); f['recovery'].assert_not_called(); f['send'].assert_not_called()
            self.assertEqual(before, sorted(str(p) for p in f['root'].rglob('*')))

    def test_real_run_and_notify_paths_send_once_suppress_duplicate_and_recover_silently(self):
        with self.fixture() as f:
            before = drill.protected_files(f['settings_path'], f['settings'])
            result = self.execute(f); summary = result['summary']
            self.assertTrue(result['completed'])
            self.assertEqual(['sent', 'suppressed', 'not-requested'],
                             [p['record']['notification'] for p in summary['phases']])
            f['send'].assert_called_once(); f['recovery'].assert_called_once()
            argv = f['send'].call_args.args[0]
            self.assertIn('통제된 LINE 알림 시험 · 운영 이상 아님', argv[argv.index('--message')+1])
            self.assertEqual('private-owner', argv[argv.index('--target')+1])
            self.assertEqual(before, drill.protected_files(f['settings_path'], f['settings']))
            self.assertNotIn('private-owner', Path(result['report']).read_text())
            directory = Path(result['report']).parent
            self.assertEqual(0o700, directory.stat().st_mode & 0o777)
            self.assertEqual({'fingerprint': None}, json.loads((directory/'runtime/incident.json').read_text()))
            for p in directory.rglob('*.json'):
                self.assertEqual(0o600, p.stat().st_mode & 0o777)
            self.assertTrue(all(c.args[1:] == ('list', '--all', '--json') for c in f['cli'].call_args_list))

    def test_unknown_or_timeout_receipt_stops_without_replay_and_preserves_evidence(self):
        for response in ('{}', '{"ok":false}', 'not-json', 'timeout'):
            with self.subTest(response=response), self.fixture() as f:
                if response == 'timeout': f['send'].side_effect = subprocess.TimeoutExpired('send', 45)
                else: f['send'].return_value.stdout = response
                result = self.execute(f)
                self.assertFalse(result['completed']); f['send'].assert_called_once(); f['recovery'].assert_not_called()
                directory = Path(result['report']).parent
                self.assertEqual('unknown', json.loads((directory/'incident-before-cleanup.json').read_text())['delivery'])
                self.assertTrue(result['summary']['isolated_incident_cleared'])
                self.assertTrue(result['summary']['production_state_unchanged'])
                self.assertTrue(result['summary']['schedule_and_job_unchanged'])

    def test_failed_real_recovery_does_not_send_another_message_or_claim_success(self):
        with self.fixture() as f:
            f['recovery'].side_effect = RuntimeError('EPUB comparison failed; private raw details')
            result = self.execute(f)
            self.assertFalse(result['completed']); f['send'].assert_called_once()
            self.assertEqual('recovery-failure-not-sent', result['summary']['phases'][-1]['record']['notification'])
            self.assertTrue(result['summary']['isolated_incident_cleared'])

    def test_broken_deduplication_cannot_send_a_second_live_message(self):
        with self.fixture() as f, patch.object(monitor, 'incident', side_effect=['first', 'first', 'changed', 'changed']):
            result = self.execute(f)
            self.assertFalse(result['completed']); f['send'].assert_called_once()
            f['recovery'].assert_not_called()
            self.assertEqual('unknown', result['summary']['phases'][-1]['record']['notification'])
            self.assertTrue(result['summary']['isolated_incident_cleared'])

    def test_cleanup_failure_cannot_be_reported_as_success(self):
        with self.fixture() as f:
            save = monitor.save
            def fail_cleanup(path, value):
                if Path(path).name == 'incident-before-cleanup.json':
                    raise OSError('cleanup denied')
                return save(path, value)
            with patch.object(monitor, 'save', side_effect=fail_cleanup):
                result = self.execute(f)
            self.assertFalse(result['completed'])
            self.assertEqual('OSError', result['summary']['cleanup_failure_type'])
            self.assertTrue(result['summary']['schedule_and_job_unchanged'])
            self.assertTrue(result['summary']['isolated_incident_cleared'])

    def test_failed_state_audit_still_attempts_schedule_verification(self):
        with self.fixture() as f:
            before = drill.protected_files(f['settings_path'], f['settings'])
            with patch.object(drill, 'protected_files', side_effect=[before, OSError('state unavailable')]):
                result = self.execute(f)
            self.assertFalse(result['completed']); f['send'].assert_called_once()
            self.assertEqual(2, f['cli'].call_count)
            self.assertTrue(result['summary']['schedule_and_job_unchanged'])
            self.assertEqual('OSError', result['summary']['state_verification_failure_type'])

    def test_incomplete_recovery_report_cannot_be_approved(self):
        with self.fixture() as f:
            f['report']['editions'][0]['public']['stable_validation']['epubcheck'] = 'not_available'
            result = self.execute(f)
            self.assertFalse(result['completed']); f['send'].assert_called_once()

    def test_active_production_lock_prevents_all_trial_operations(self):
        with self.fixture() as f, file_lock(str(f['runtime']/'monitor.lock')):
            with self.assertRaises(BlockingIOError): self.execute(f)
            f['cli'].assert_not_called(); f['send'].assert_not_called()

    def test_bad_or_imminent_jobs_are_rejected_before_sending(self):
        for problem in ('active', 'imminent', 'foreign', 'disabled', 'native_delivery', 'failure_alert',
                        'schedule', 'command', 'environment', 'timeout', 'duplicate', 'wrong_id'):
            with self.subTest(problem=problem), self.fixture() as f:
                j = f['job']
                if problem == 'active': j['state'] = {'runningAtMs': 1}
                elif problem == 'imminent': j['nextRunAtMs'] = int(time.time()*1000)+1000
                elif problem == 'foreign': j['declarationKey'] = 'other'
                elif problem == 'disabled': j['enabled'] = False
                elif problem == 'native_delivery': j['delivery']['mode'] = 'announce'
                elif problem == 'failure_alert': j['failureAlert'] = True
                elif problem == 'schedule': j['schedule']['tz'] = 'UTC'
                elif problem == 'command': j['payload']['argv'] = ['another-script']
                elif problem == 'environment': j['payload']['env']['JAVA_HOME'] = '/broken'
                elif problem == 'timeout': j['payload']['noOutputTimeoutSeconds'] = 60
                elif problem == 'wrong_id': j['id'] = 'other'
                else: f['cli'].side_effect = lambda *a: {'jobs': [j, j]}
                with self.assertRaises(ValueError): self.execute(f)
                f['send'].assert_not_called(); f['recovery'].assert_not_called()

    def test_schedule_drift_or_gateway_failure_is_not_success_and_is_not_automatically_overwritten(self):
        for problem in ('schedule', 'unreachable'):
            with self.subTest(problem=problem), self.fixture() as f:
                original = copy.deepcopy(f['job']); changed = copy.deepcopy(original)
                changed['schedule']['expr'] = '0 10 * * *'
                f['cli'].side_effect = [{'jobs': [original]}, RuntimeError('private gateway error')
                                       if problem == 'unreachable' else {'jobs': [changed]}]
                result = self.execute(f)
                self.assertFalse(result['completed']); f['send'].assert_called_once()
                self.assertEqual(2, f['cli'].call_count)
                self.assertTrue(result['summary']['isolated_incident_cleared'])
                self.assertNotIn('private gateway error', Path(result['report']).read_text())

    def test_external_state_changes_are_detected_and_never_restored_over(self):
        with self.fixture() as f:
            original_checker = f['checker']
            def changed(s, report, log):
                (f['runtime']/'external.json').write_text('{"external": true}')
                return original_checker(s, report, log)
            f['recovery'].side_effect = changed
            result = self.execute(f)
            self.assertFalse(result['completed']); self.assertFalse(result['summary']['production_state_unchanged'])
            self.assertTrue((f['runtime']/'external.json').exists())

    def test_overlapping_runtime_paths_are_rejected(self):
        with self.fixture() as f:
            f['settings']['runtime_dir'] = str(f['data'])
            monitor.save(f['settings_path'], f['settings'])
            with self.assertRaisesRegex(ValueError, 'overlap'): self.execute(f)
            f['send'].assert_not_called()

    def test_production_symlink_is_rejected(self):
        with self.fixture() as f:
            p = f['runtime']/'incident.json'; p.unlink(); p.symlink_to(f['manifest'])
            with self.assertRaisesRegex(ValueError, 'Symlink'): self.execute(f)
            f['send'].assert_not_called()

    def test_production_lock_is_held_through_recovery_and_released_afterwards(self):
        with self.fixture() as f:
            original = f['checker']
            def checker(s, report, log):
                with self.assertRaises(BlockingIOError):
                    with file_lock(str(f['runtime']/'monitor.lock'), blocking=False): pass
                return original(s, report, log)
            f['recovery'].side_effect = checker
            self.assertTrue(self.execute(f)['completed'])
            with file_lock(str(f['runtime']/'monitor.lock'), blocking=False): pass


if __name__ == '__main__':
    unittest.main()
