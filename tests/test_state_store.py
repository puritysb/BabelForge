from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import ExitStack
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import catalog
import config
import pending_queue
from state_store import StateError


def _create_requests(args):
    path, count = args
    config.CATALOG_PATH = path
    return [catalog.new_request(f'book {i}') for i in range(count)]


def _queue_book(args):
    path, epub = args
    config.PENDING_PUSH_PATH = path
    pending_queue.enqueue(epub, 'Book', 'Author')


class StateTransactionsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = os.path.join(self.directory, 'catalog.json')
        self.queue = os.path.join(self.directory, 'pending.json')
        for key, value in {'CATALOG_PATH': self.path, 'PENDING_PUSH_PATH': self.queue,
                           'CHECKPOINT_DIR': self.directory}.items():
            self.stack.enter_context(patch.object(config, key, value))

    def test_multiple_processes_preserve_all_requests(self):
        with ProcessPoolExecutor(4, mp_context=multiprocessing.get_context('spawn')) as ex:
            ids = sum(ex.map(_create_requests, [(self.path, 8)] * 4), [])
        self.assertEqual(32, len(catalog.recent(100)))
        self.assertEqual(set(ids), {e['id'] for e in catalog.recent(100)})

    def test_multiple_processes_preserve_all_queued_books(self):
        epubs = [os.path.join(self.directory, f'{i}.epub') for i in range(12)]
        for epub in epubs:
            Path(epub).touch()
        with ProcessPoolExecutor(4, mp_context=multiprocessing.get_context('spawn')) as ex:
            list(ex.map(_queue_book, [(self.queue, epub) for epub in epubs]))
        self.assertEqual(set(epubs), {e['epub_path'] for e in pending_queue.list_pending()})

    def test_queue_preserves_explicit_notification_choice_and_defaults_silent(self):
        epub = os.path.join(self.directory, 'book.epub')
        Path(epub).touch()
        pending_queue.enqueue(epub, 'Book', 'Author')
        self.assertIs(pending_queue.list_pending()[0]['notify'], False)
        pending_queue.enqueue(epub, 'Book', 'Author', notify=True)
        self.assertEqual(1, len(pending_queue.list_pending()))
        self.assertIs(pending_queue.list_pending()[0]['notify'], True)

    def test_corrupt_catalog_and_queue_are_preserved(self):
        for path, operation in [(self.path, lambda: catalog.new_request('book')),
                                (self.queue, lambda: pending_queue.remove('book.epub'))]:
            for raw in ['{broken', '{}', 'null']:
                Path(path).write_text(raw)
                with self.assertRaises(StateError):
                    operation()
                self.assertEqual(raw, Path(path).read_text())

    def test_parallel_updates_to_different_requests_survive(self):
        ids = [catalog.new_request(str(i)) for i in range(20)]
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(lambda req: catalog.update(req, status='completed'), ids))
        self.assertTrue(all(e['status'] == 'completed' for e in catalog.recent(100)))

    def test_retry_claim_is_atomic(self):
        req = catalog.new_request('book')
        catalog.update(req, status='failed', selected={'title': 'Book'})
        def claim(_):
            try:
                catalog.claim_retry(req)
                return True
            except catalog.RequestAlreadyRunning:
                return False
        with ThreadPoolExecutor(8) as ex:
            outcomes = list(ex.map(claim, range(8)))
        self.assertEqual(1, sum(outcomes))
        self.assertEqual(1, catalog.get(req)['retry_count'])

    def test_worker_lock_rejects_duplicate_and_releases(self):
        req = catalog.new_request('book')
        with catalog.request_worker(req):
            with self.assertRaises(catalog.RequestAlreadyRunning):
                with catalog.request_worker(req):
                    self.fail('duplicate worker entered')
            self.assertEqual(os.getpid(), catalog.get(req)['worker_pid'])
        with catalog.request_worker(req):
            pass
        self.assertIsNone(catalog.get(req)['worker_pid'])

    def test_dead_worker_can_resume_without_new_request(self):
        req = catalog.new_request('book')
        catalog.update(req, status='translating', selected={'title': 'Book'},
                       worker_pid=999999999, worker_started_ms=1)
        self.assertEqual('failed', catalog.reconcile_worker(req)['status'])
        self.assertEqual({'title': 'Book'}, catalog.claim_retry(req))
        self.assertEqual('launching', catalog.get(req)['status'])

    def test_completed_local_request_is_terminal(self):
        req = catalog.new_request('book')
        catalog.mark_completed(req, '/tmp/book.epub')
        with self.assertRaises(ValueError):
            catalog.claim_retry(req)
        self.assertEqual('completed', catalog.reconcile_worker(req)['status'])

    def test_late_spawn_bookkeeping_does_not_reopen_finished_worker(self):
        req = catalog.new_request('book')
        catalog.prepare_worker(req, {'title': 'Book'})
        with catalog.request_worker(req):
            catalog.mark_completed(req, '/tmp/book.epub')
        catalog.record_worker_spawn(req, 999999999)
        self.assertIsNone(catalog.get(req)['worker_pid'])
        self.assertEqual('completed', catalog.get(req)['status'])

    def test_worker_cleanup_failure_does_not_mask_success(self):
        req = catalog.new_request('book')
        broken = patch.object(catalog, '_transaction', side_effect=OSError('cleanup unavailable'))
        try:
            with catalog.request_worker(req):
                catalog.mark_completed(req, '/tmp/book.epub')
                broken.start()
        finally:
            broken.stop()
        self.assertFalse(catalog._worker_locked(req))
        self.assertEqual('completed', catalog.get(req)['status'])

    def test_unexpected_worker_exception_is_recoverable(self):
        req = catalog.new_request('book')
        catalog.update(req, selected={'title': 'Book'})
        with self.assertRaisesRegex(RuntimeError, 'unexpected fixture failure'):
            with catalog.request_worker(req):
                catalog.update(req, status='translating')
                raise RuntimeError('unexpected fixture failure')
        self.assertEqual('failed', catalog.get(req)['status'])
        self.assertIsNone(catalog.get(req)['worker_pid'])
        self.assertEqual({'title': 'Book'}, catalog.claim_retry(req))
