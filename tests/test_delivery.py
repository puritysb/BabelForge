from __future__ import annotations
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import auto_push_watcher
import catalog
import config
import device_push
import search


class DeviceIdentityTests(unittest.TestCase):
    def _mdns(self, is_crosspoint):
        fake = ModuleType('zeroconf')
        class Zeroconf:
            def get_service_info(self, typ, name, timeout):
                return SimpleNamespace(addresses=[socket.inet_aton('192.0.2.25')])
            def close(self):
                pass
        def browser(zc, typ, listener):
            if typ == '_http._tcp.local.':
                listener.add_service(zc, typ, 'Office Printer._http._tcp.local.')
        fake.Zeroconf, fake.ServiceBrowser = Zeroconf, browser
        with patch.dict(sys.modules, {'zeroconf': fake}), patch.object(device_push.time, 'sleep'), patch.object(device_push, '_looks_like_crosspoint', return_value=is_crosspoint):
            return device_push._discover_via_mdns()

    def test_unrelated_mdns_service_is_rejected(self):
        self.assertIsNone(self._mdns(False))

    def test_verified_mdns_service_is_selected(self):
        self.assertEqual('192.0.2.25', self._mdns(True))

    def test_generic_file_manager_is_not_a_device_identity(self):
        with patch.object(device_push.requests, 'get', return_value=SimpleNamespace(status_code=200, content=b'NAS file manager file transfer')):
            self.assertFalse(device_push._looks_like_crosspoint('192.0.2.25'))
            self.assertFalse(device_push._server_alive('http://192.0.2.25', 1))

    def test_subnet_widening_reaches_adjacent_subnets(self):
        hosts = device_push._subnet_hosts('192.168.1.2', 22)
        self.assertEqual(1022, len(hosts))
        self.assertIn('192.168.3.254', hosts)

    def test_disabled_watcher_never_discovers_or_sends(self):
        with patch.object(config, 'DEVICE_PUSH_ENABLED', False), patch.object(auto_push_watcher, 'discover_device') as discover, patch.object(auto_push_watcher.search_mod, 'notify_line') as notify:
            self.assertTrue(auto_push_watcher.drain_once()['disabled'])
        discover.assert_not_called()
        notify.assert_not_called()

    def test_successful_watcher_updates_catalog_delivery(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, 'DATA_DIR', directory), patch.object(config, 'CATALOG_PATH', os.path.join(directory, 'catalog.json')), patch.object(config, 'DEVICE_PUSH_ENABLED', True):
            req = catalog.new_request('Book')
            epub = os.path.join(directory, 'book.epub')
            Path(epub).touch()
            queued = [{'req_id': req, 'epub_path': epub, 'title': 'Book'}]
            result = device_push.PushResult(pushed=True, path='/Books', elapsed_ms=1)
            with patch.object(auto_push_watcher.pending_queue, 'list_pending', return_value=queued), patch.object(auto_push_watcher.pending_queue, 'increment_attempts'), patch.object(auto_push_watcher.pending_queue, 'remove') as remove, patch.object(auto_push_watcher, 'discover_device', return_value=('192.0.2.25', 'fixture')), patch.object(auto_push_watcher, 'push_to_device', return_value=result), patch.object(auto_push_watcher.search_mod, 'notify_line') as notify:
                summary = auto_push_watcher.drain_once()
            self.assertEqual(1, summary['pushed'])
            self.assertTrue(catalog.get(req)['device_push']['pushed'])
            remove.assert_called_once_with(epub)
            notify.assert_not_called()  # legacy queue entry has no approval

    def test_watcher_only_notifies_opted_in_books_in_mixed_batch(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, 'DATA_DIR', directory), patch.object(config, 'DEVICE_PUSH_ENABLED', True):
            queued = []
            for title, settings in [('Quiet Book', {'notify': False}), ('Legacy Book', {}),
                                    ('Announced Book', {'notify': True})]:
                epub = os.path.join(directory, title + '.epub')
                Path(epub).touch()
                queued.append({'epub_path': epub, 'title': title, **settings})
            result = device_push.PushResult(pushed=True, elapsed_ms=1)
            with patch.object(auto_push_watcher.pending_queue, 'list_pending', return_value=queued), patch.object(auto_push_watcher.pending_queue, 'increment_attempts'), patch.object(auto_push_watcher.pending_queue, 'remove'), patch.object(auto_push_watcher, 'discover_device', return_value=('192.0.2.25', 'fixture')), patch.object(auto_push_watcher, 'push_to_device', return_value=result), patch.object(auto_push_watcher.search_mod, 'notify_line') as notify:
                summary = auto_push_watcher.drain_once()
            self.assertEqual(3, summary['pushed'])
            messages = '\n'.join(call.args[0] for call in notify.call_args_list)
            self.assertEqual(3, notify.call_count)
            self.assertIn('Announced Book', messages)
            self.assertNotIn('Quiet Book', messages)
            self.assertNotIn('Legacy Book', messages)
            self.assertIn('1권', messages)

    def test_watcher_suppresses_failures_for_silent_requests(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, 'DATA_DIR', directory), patch.object(config, 'DEVICE_PUSH_ENABLED', True):
            epub = os.path.join(directory, 'quiet.epub')
            Path(epub).touch()
            queued = [{'epub_path': epub, 'title': 'Quiet Book', 'notify': False}]
            result = device_push.PushResult(pushed=False, skipped=False, error='fixture failure')
            with patch.object(auto_push_watcher.pending_queue, 'list_pending', return_value=queued), patch.object(auto_push_watcher.pending_queue, 'increment_attempts'), patch.object(auto_push_watcher, 'discover_device', return_value=('192.0.2.25', 'fixture')), patch.object(auto_push_watcher, 'push_to_device', return_value=result), patch.object(auto_push_watcher.search_mod, 'notify_line') as notify:
                summary = auto_push_watcher.drain_once()
            self.assertEqual(1, summary['failed'])
            notify.assert_not_called()


class EntryPointTests(unittest.TestCase):
    def test_notification_timeout_is_nonfatal(self):
        with patch.object(search.os.path, 'exists', return_value=True), patch.object(search.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixture notifier', 30)):
            self.assertFalse(search.notify_line('fixture message; transport is mocked'))

    def test_cli_accepts_dotenv_without_exported_key(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / 'venv' / 'bin').mkdir(parents=True)
            (work / 'venv' / 'bin' / 'python3').symlink_to(sys.executable)
            shutil.copy(root / 'run_request.sh', work)
            shutil.copy(root / 'config.py', work)
            (work / '.env').write_text('GLM_API_KEY=fixture-test-only\n')
            (work / 'pipeline.py').write_text('import config\nassert config.get_zai_api_key() == "fixture-test-only"\n')
            env = dict(os.environ)
            env.pop('GLM_API_KEY', None)
            env.pop('ZAI_API_KEY', None)
            result = subprocess.run(['bash', str(work / 'run_request.sh'), 'fixture.json'], env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
