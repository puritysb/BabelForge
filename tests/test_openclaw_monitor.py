"""No gateway, Java, LINE, production books, or real subprocesses are used."""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

import config
import java_runtime as java
import openclaw_monitor as monitor
import install_openclaw_monitor as installer
from state_store import file_lock


class Fixture:
    @contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); runtime = root/'runtime'; runtime.mkdir()
            manifest = root/'manifest.json'; manifest.write_text('{}')
            check = {'pairs': 3, 'passed': True, 'epubcheck': 'passed', 'content_compared': True}
            report = {'completed': True, 'stage': 'completed', 'mode': 'check', 'finished_at': 'now',
                      'manifest': str(manifest), 'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
                      'semantic_version': monitor.SEMANTIC_AUDIT_VERSION, 'approved_pairs': 3,
                      'inventory': {'managed_editions': 1, 'opds_ids': [7], 'duplicates': 0, 'obsolete_deliverables': 0},
                      'editions': [{'id': 'book', 'book_id': 7, 'sha256': 'approved', 'validation': check,
                                    'public': {'validation': check, 'stable_validation': check}}],
                      **{k: {'editions': 1} for k in ['mcp_before', 'mcp_after', 'mcp_final']}}
            expected = monitor.baseline(report, manifest)
            settings = {'version': 1, 'manifest': str(manifest), 'expected': expected,
                        'runtime_dir': str(runtime), 'java_home': '/jdk', 'line_target': 'approved-test-owner',
                        'openclaw': '/test/openclaw', 'timezone': 'Asia/Seoul', 'check_timeout': 1200}
            report_file = root/'baseline.json'; report_file.write_text(json.dumps(report))
            binary = root/'openclaw'; binary.write_text('test')
            def check_command(s, path, log):
                path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(report)); return 0
            with patch.object(config, 'BASE_DIR', str(root)), patch.object(config, 'DATA_DIR', str(root/'data')), \
                    patch.object(monitor, 'check_command', side_effect=check_command) as checker, \
                    patch.object(monitor.subprocess, 'run', return_value=subprocess.CompletedProcess(
                        [], 0, '{"payload":{"messageId":"confirmed"}}', '')) as send, \
                    patch('builtins.print'):
                yield locals()


class MonitorTests(Fixture, unittest.TestCase):
    def failure(self, f, error='EPUBCheck failed'):
        f['checker'].side_effect = RuntimeError(error)

    def test_healthy_and_recovered_runs_send_nothing(self):
        with self.fixture() as f:
            self.assertEqual(0, monitor.run(f['settings'])); f['send'].assert_not_called()
            self.failure(f); self.assertEqual(1, monitor.run(f['settings'])); self.assertEqual(1, f['send'].call_count)
            f['checker'].side_effect = f['check_command']
            self.assertEqual(0, monitor.run(f['settings'])); self.assertEqual(1, f['send'].call_count)

    def test_deduplication_changed_cause_and_recurrence_after_recovery(self):
        with self.fixture() as f:
            self.failure(f)
            for _ in range(2): self.assertEqual(1, monitor.run(f['settings']))
            self.assertEqual(1, f['send'].call_count)
            self.failure(f, 'ConnectionRefusedError'); monitor.run(f['settings'])
            self.assertEqual(2, f['send'].call_count)
            f['checker'].side_effect = f['check_command']; monitor.run(f['settings'])
            self.failure(f, 'ConnectionRefusedError'); monitor.run(f['settings'])
            self.assertEqual(3, f['send'].call_count)

    def test_uuid_and_temporary_paths_do_not_create_new_incidents(self):
        with self.fixture() as f:
            a = 'EPUBCheck babelforge-deploy-abc/a openclaw-12345678-1234-1234-1234-123456789abc.json'
            b = 'EPUBCheck babelforge-deploy-def/a openclaw-abcdef12-1234-1234-1234-123456789abc.json'
            self.assertEqual(monitor.incident(a, 'public_validation', f['expected']),
                             monitor.incident(b, 'public_validation', f['expected']))

    def test_corrupt_state_does_not_silence_first_alert(self):
        for text in ('{broken', '[]'):
            with self.subTest(text=text), self.fixture() as f:
                (f['runtime']/'incident.json').write_text(text)
                self.failure(f); monitor.run(f['settings']); f['send'].assert_called_once()

    def test_unknown_send_result_is_not_retried_or_claimed_delivered(self):
        for response in ('{}', '{"ok":false}', '{"messageId":true}'):
            with self.subTest(response=response), self.fixture() as f:
                f['send'].return_value.stdout = response
                self.failure(f); monitor.run(f['settings']); monitor.run(f['settings'])
                self.assertEqual(1, f['send'].call_count)
                state = json.loads((f['runtime']/'incident.json').read_text())
                self.assertEqual('unknown', state['delivery'])

    def test_send_timeout_keeps_unknown_receipt_and_no_automatic_duplicate(self):
        with self.fixture() as f:
            f['send'].side_effect = subprocess.TimeoutExpired('openclaw', 45)
            self.failure(f); monitor.run(f['settings']); monitor.run(f['settings'])
            self.assertEqual(1, f['send'].call_count)
            self.assertEqual('unknown', json.loads((f['runtime']/'incident.json').read_text())['delivery'])

    def test_notification_never_contains_raw_book_text_or_credentials(self):
        with self.fixture() as f:
            self.failure(f, 'EPUBCheck failed: secret=private-token BOOK-PASSAGE-NEVER-SEND')
            monitor.run(f['settings'])
            argv = f['send'].call_args.args[0]; message = argv[argv.index('--message')+1]
            self.assertNotIn('private-token', message); self.assertNotIn('BOOK-PASSAGE', message)
            self.assertIn('원인:', message); self.assertIn('보고서:', message)
            self.assertEqual('approved-test-owner', argv[argv.index('--target')+1])

    def test_manual_run_uses_installed_cli_runtime_path_for_notifications(self):
        with self.fixture() as f:
            f['settings']['execution_path'] = '/configured-java/bin:/configured-node/bin:/usr/bin'
            self.failure(f); monitor.run(f['settings'])
            self.assertEqual(f['settings']['execution_path'], f['send'].call_args.kwargs['env']['PATH'])

    def test_complete_flag_cannot_hide_bad_or_incomplete_results(self):
        for problem in ('stable', 'hash', 'inventory', 'pairs', 'version', 'mcp'):
            with self.subTest(problem=problem), self.fixture() as f:
                report = copy.deepcopy(f['report'])
                if problem == 'stable': report['editions'][0]['public']['stable_validation']['epubcheck'] = 'not_available'
                elif problem == 'hash': report['editions'][0]['sha256'] = 'different'
                elif problem == 'inventory': report['inventory']['duplicates'] = 1
                elif problem == 'pairs': report['approved_pairs'] = 2
                elif problem == 'version': report['semantic_version'] = 'obsolete'
                else: report['mcp_final']['editions'] = 0
                with self.assertRaises(ValueError): monitor.validate(report, f['expected'], f['manifest'])

    def test_nonzero_command_cannot_pass_with_an_old_completed_report(self):
        with self.fixture() as f:
            def fail(s, path, log): f['check_command'](s, path, log); return 1
            f['checker'].side_effect = fail
            self.assertEqual(1, monitor.run(f['settings'])); f['send'].assert_called_once()

    def test_overlapping_monitor_does_not_launch_checker_or_duplicate_alert(self):
        with self.fixture() as f, file_lock(str(f['runtime']/'monitor.lock')):
            with self.assertRaises(BlockingIOError): monitor.run(f['settings'])
            f['checker'].assert_not_called(); f['send'].assert_not_called()

    def test_state_files_use_private_atomic_writes(self):
        with self.fixture() as f:
            monitor.run(f['settings'])
            self.assertEqual(0o600, (f['runtime']/'incident.json').stat().st_mode & 0o777)

class JavaTests(unittest.TestCase):
    def test_explicit_compatible_runtime_sets_path_before_system_launcher(self):
        with patch.object(java.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', 'openjdk version "17.0.19"')):
            env = java.java_environment('/approved-jdk')
            self.assertEqual('/approved-jdk', env['JAVA_HOME'])
            self.assertTrue(env['PATH'].startswith('/approved-jdk/bin'+os.pathsep))

    def test_wrong_architecture_or_old_explicit_runtime_fails_closed(self):
        for reply in (OSError('Bad CPU type'), subprocess.CompletedProcess([], 0, '', 'java version "1.8.0"')):
            with self.subTest(reply=str(reply)), patch.object(java.subprocess, 'run') as run:
                if isinstance(reply, Exception): run.side_effect = reply
                else: run.return_value = reply
                with self.assertRaisesRegex(RuntimeError, 'Java 17'): java.java_environment('/broken-jdk')

    def test_discovery_skips_broken_inherited_java_home(self):
        with patch.dict(os.environ, {'JAVA_HOME': '/old-jdk'}), patch.object(java.sys, 'platform', 'linux'), \
                patch.object(java.shutil, 'which', side_effect=lambda name: '/new-jdk/bin/java' if name == 'java' else None), \
                patch.object(java.subprocess, 'run', side_effect=[OSError('Bad CPU type'),
                    subprocess.CompletedProcess([], 0, '', 'openjdk version "21.0.1"')]):
            self.assertEqual('/new-jdk', java.java_environment()['JAVA_HOME'])

    def test_timeout_kills_java_descendants(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(monitor, 'java_environment', return_value={'PATH': '/jdk/bin'}), \
                patch.object(monitor.subprocess, 'Popen') as popen, patch.object(monitor.os, 'killpg') as kill:
            process = popen.return_value; process.pid = 12345
            process.wait.side_effect = [subprocess.TimeoutExpired('check', 10), None]
            with self.assertRaises(TimeoutError): monitor.check_command(
                {'java_home': '/jdk', 'manifest': '/manifest', 'check_timeout': 10}, Path(tmp)/'report', Path(tmp)/'log')
            kill.assert_called_once_with(12345, monitor.signal.SIGTERM)
            self.assertTrue(popen.call_args.kwargs['start_new_session'])


class InstallationTests(Fixture, unittest.TestCase):
    def test_obsolete_baseline_version_is_rejected_before_installation(self):
        with self.fixture() as f:
            f['report']['semantic_version'] = 'obsolete-version'
            with self.assertRaisesRegex(ValueError, 'Obsolete baseline'):
                monitor.baseline(f['report'], f['manifest'])

    def test_boolean_counts_and_ids_cannot_certify_a_completed_report(self):
        for field in ('managed_editions', 'duplicates', 'opds_ids', 'book_id', 'mcp'):
            with self.subTest(field=field), self.fixture() as f:
                report = copy.deepcopy(f['report'])
                if field == 'managed_editions': report['inventory'][field] = True
                elif field == 'duplicates': report['inventory'][field] = False
                elif field == 'opds_ids': report['inventory'][field] = [True]
                elif field == 'book_id': report['editions'][0][field] = True
                else: report['mcp_final']['editions'] = True
                with self.assertRaises(ValueError): monitor.baseline(report, f['manifest'])

    def test_same_name_with_foreign_declaration_is_never_adopted(self):
        with self.fixture() as f, patch.object(installer, 'cli') as cli:
            for declaration, requested in ((None, None), ('another-declaration', None), ('another-declaration', 'one')):
                cli.reset_mock(); cli.return_value = {'jobs': [{'id': 'one', 'name': installer.JOB_NAME,
                                                               'declarationKey': declaration}]}
                with self.assertRaisesRegex(ValueError, 'matching name alone'):
                    installer.reconcile(f['settings'], f['root']/'settings', job_id=requested)
                cli.assert_called_once()
                self.assertFalse((f['root']/'settings').exists())

    def test_reinstall_preserves_job_id_and_incident_cache(self):
        with self.fixture() as f:
            settings_path = f['root']/'settings.json'; state = f['runtime']/'incident.json'; state.write_text('{"fingerprint":"existing"}')
            argv = [config.VENV_PYTHON, str(f['root']/'openclaw_monitor.py'), '--config', str(settings_path.resolve())]
            job = {'id': 'existing-job', 'name': installer.JOB_NAME, 'declarationKey': installer.DECLARATION,
                   'enabled': True, 'failureAlert': False, 'delivery': {'mode': 'none'},
                   'schedule': {'kind': 'cron', 'expr': '0 9 * * *', 'tz': 'Asia/Seoul', 'staggerMs': 0},
                   'payload': {'argv': argv, 'timeoutSeconds': 1500, 'noOutputTimeoutSeconds': 1500}}
            def cli(binary, action, *args):
                if action == 'list': return {'jobs': [job]}
                if action == 'get': return job
                self.assertEqual('edit', action); return job
            with patch.object(installer, 'cli', side_effect=cli) as calls:
                for _ in range(2):
                    self.assertEqual('existing-job', installer.reconcile(f['settings'], settings_path)['job_id'])
                self.assertFalse(any(c.args[1] == 'add' for c in calls.call_args_list))
                flags = next(c.args for c in calls.call_args_list if c.args[1] == 'edit')
                self.assertIn('--no-failure-alert', flags); self.assertIn('--no-deliver', flags)
            self.assertEqual('{"fingerprint":"existing"}', state.read_text())

    def test_ambiguous_or_unrelated_job_is_not_modified(self):
        with self.fixture() as f, patch.object(installer, 'cli') as cli:
            job = {'id': 'one', 'name': installer.JOB_NAME}
            for jobs, requested in (([job, dict(job, id='two')], None), ([job], 'another-job')):
                cli.return_value = {'jobs': jobs}
                with self.assertRaises(ValueError): installer.reconcile(f['settings'], f['root']/'settings', job_id=requested)
                self.assertTrue(all(c.args[1] == 'list' for c in cli.call_args_list))

    def test_prepare_pins_actual_inventory_without_private_paths_in_source(self):
        with self.fixture() as f, patch.object(installer, 'java_environment', return_value={
                'JAVA_HOME': '/valid-jdk', 'PATH': '/node-runtime/bin:/usr/bin'}):
            settings = installer.prepare(f['manifest'], f['report_file'], 'approved-owner', openclaw=str(f['binary']))
            self.assertEqual(1, len(settings['expected']['editions']))
            self.assertEqual(3, settings['expected']['approved_pairs'])
            self.assertIn('/node-runtime/bin', settings['execution_path'])
            f['manifest'].write_text('{"changed":true}')
            with self.assertRaisesRegex(ValueError, 'manifest changed'):
                installer.prepare(f['manifest'], f['report_file'], 'approved-owner', openclaw=str(f['binary']))

    def test_new_install_is_disabled_until_failure_delivery_is_explicitly_off(self):
        with self.fixture() as f:
            settings_path = f['root']/'settings.json'
            current = {'id': 'new-job', 'enabled': True, 'failureAlert': False, 'delivery': {'mode': 'none'},
                       'schedule': {'kind': 'cron', 'expr': '0 9 * * *', 'tz': 'Asia/Seoul', 'staggerMs': 0},
                       'payload': {'argv': [config.VENV_PYTHON, str(f['root']/'openclaw_monitor.py'),
                                           '--config', str(settings_path.resolve())],
                                   'timeoutSeconds': 1500, 'noOutputTimeoutSeconds': 1500}}
            with patch.object(installer, 'cli', side_effect=[{'jobs': []}, {'job': {'id': 'new-job'}}, current, current]) as cli:
                self.assertEqual('new-job', installer.reconcile(f['settings'], settings_path)['job_id'])
                self.assertIn('--disabled', cli.call_args_list[1].args)
                self.assertIn('--no-failure-alert', cli.call_args_list[2].args)
                self.assertIn('--enable', cli.call_args_list[2].args)


if __name__ == '__main__':
    unittest.main()
