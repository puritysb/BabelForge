from __future__ import annotations
from contextlib import ExitStack
import os
import tempfile
import unittest
from unittest.mock import patch

import babelforge_mcp
import catalog
import config


class RetryRequestTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        for name in ['DATA_DIR', 'CACHE_DIR', 'LOGS_DIR', 'CHECKPOINT_DIR']:
            self.stack.enter_context(patch.object(config, name, directory))
        self.stack.enter_context(patch.object(config, 'CATALOG_PATH', os.path.join(directory, 'catalog.json')))
        self.stack.enter_context(patch.object(config, 'ensure_dirs'))
        self.req = catalog.new_request('Book')
        self.candidate = {'source': 'gutenberg', 'source_id': '7207', 'title': 'Book',
                          'author': 'Nietzsche', 'language': 'de'}
        catalog.update(self.req, status='failed', selected=self.candidate, retry_count=1)

    def test_failed_request_reuses_same_id_and_candidate(self):
        with patch.object(babelforge_mcp, '_spawn_pipeline', return_value={'req_id': self.req}) as spawn:
            result = babelforge_mcp.retry_request(self.req, notify=False)
        self.assertEqual('retrying', result['status'])
        spawn.assert_called_once_with(self.candidate, self.req, False, True)
        self.assertEqual(2, catalog.get(self.req)['retry_count'])

    def test_active_worker_cannot_spawn_duplicate(self):
        with catalog.request_worker(self.req), patch.object(babelforge_mcp, '_spawn_pipeline') as spawn:
            result = babelforge_mcp.retry_request(self.req)
        self.assertIn('running', result['error'])
        spawn.assert_not_called()

    def test_local_output_flag_reaches_child(self):
        with patch.object(babelforge_mcp.subprocess, 'Popen') as popen:
            popen.return_value.pid = 999999999
            babelforge_mcp.retry_request(self.req, notify=False, publish_output=False)
        argv = popen.call_args.args[0]
        self.assertIn('--no-publish', argv)
        self.assertIn('--no-notify', argv)

    def test_spawn_failure_is_immediately_retryable(self):
        with patch.object(babelforge_mcp.subprocess, 'Popen', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError):
                babelforge_mcp.retry_request(self.req)
        self.assertEqual('failed', catalog.get(self.req)['status'])
        self.assertEqual(self.candidate, catalog.claim_retry(self.req))

    def test_retry_preserves_local_only_output(self):
        catalog.update(self.req, publish_output=False)
        with patch.object(babelforge_mcp, '_spawn_pipeline', return_value={'req_id': self.req}) as spawn:
            babelforge_mcp.retry_request(self.req)
        spawn.assert_called_once_with(self.candidate, self.req, False, False)

    def test_retry_defaults_legacy_notification_setting_to_silent(self):
        with patch.object(babelforge_mcp, '_spawn_pipeline', return_value={'req_id': self.req}) as spawn:
            babelforge_mcp.retry_request(self.req)
        spawn.assert_called_once_with(self.candidate, self.req, False, True)
        self.assertIs(catalog.get(self.req)['notify'], False)

    def test_retry_preserves_explicit_notification_choices(self):
        for allowed in (False, True):
            with self.subTest(notify=allowed):
                catalog.update(self.req, status='failed', worker_claimed_ms=None, notify=allowed)
                with patch.object(babelforge_mcp, '_spawn_pipeline', return_value={'req_id': self.req}) as spawn:
                    babelforge_mcp.retry_request(self.req)
                spawn.assert_called_once_with(self.candidate, self.req, allowed, True)

    def test_retry_can_explicitly_change_notification_choice(self):
        catalog.update(self.req, notify=True)
        with patch.object(babelforge_mcp, '_spawn_pipeline', return_value={'req_id': self.req}) as spawn:
            babelforge_mcp.retry_request(self.req, notify=False)
        spawn.assert_called_once_with(self.candidate, self.req, False, True)
        self.assertIs(catalog.get(self.req)['notify'], False)

    def test_new_request_persists_notification_choice_before_spawn(self):
        with patch.object(babelforge_mcp, '_spawn_pipeline', return_value={}) as spawn:
            babelforge_mcp.translate_book(self.candidate, notify=False, publish_output=False)
        req = spawn.call_args.args[1]
        self.assertIs(catalog.get(req)['notify'], False)
        self.assertIs(catalog.get(req)['publish_output'], False)

    def test_status_recovers_dead_worker(self):
        catalog.update(self.req, status='translating', worker_pid=999999999,
                       worker_started_ms=1)
        self.assertEqual('failed', babelforge_mcp.get_status(self.req)['status'])

    def test_list_recovers_legacy_orphan_without_spawning(self):
        with patch.object(catalog.time, 'time', return_value=1):
            catalog.update(self.req, status='translating')
        with patch.object(babelforge_mcp, '_spawn_pipeline') as spawn:
            self.assertEqual('failed', babelforge_mcp.list_recent()[0]['status'])
        spawn.assert_not_called()

    def test_server_startup_recovers_existing_orphans(self):
        with patch.object(catalog.time, 'time', return_value=1):
            catalog.update(self.req, status='translating')
        with patch.object(babelforge_mcp.sys, 'argv', ['babelforge_mcp.py']), \
             patch.object(babelforge_mcp.mcp, 'run') as run:
            babelforge_mcp.main()
            run.assert_called_once_with(transport='stdio')
        self.assertEqual('failed', catalog.get(self.req)['status'])
