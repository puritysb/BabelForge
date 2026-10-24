from __future__ import annotations

from contextlib import ExitStack, contextmanager
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import catalog
import config
import pipeline
from extract import Chapter
from translate import TranslatedBook, TranslatedChapter


class PipelinePrecisionTests(unittest.TestCase):
    @contextmanager
    def fixture(self, *, device=False):
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            root = Path(temp_dir)
            for key, suffix in {
                'CATALOG_PATH': 'catalog.json', 'PENDING_PUSH_PATH': 'pending.json',
                'CONCEPT_DIR': 'concepts', 'CHECKPOINT_DIR': 'checkpoints',
                'LIBRARY_DIR': 'library',
            }.items():
                stack.enter_context(patch.object(config, key, str(root / suffix)))
            stack.enter_context(patch.object(config, 'DEVICE_PUSH_ENABLED', device))
            source = root / 'source.txt'
            source.write_text('Hello, my friend.')
            candidate = {
                'source': 'gutenberg', 'source_id': '1', 'title': 'Portable Test',
                'author': 'Author', 'language': 'en', 'rights': 'public domain',
                'fetch_url': 'https://example.test/source.txt',
            }
            sheet = {'profile_hash': 'fixture-profile', 'concepts': []}
            stack.enter_context(patch.object(pipeline, 'research_source', return_value={}))
            stack.enter_context(patch.object(pipeline, 'choose_original_candidate',
                                            return_value=(candidate, False)))
            stack.enter_context(patch.object(pipeline, 'build_concept_sheet', return_value=sheet))
            stack.enter_context(patch.object(pipeline, 'persist_concept_sheet'))
            stack.enter_context(patch.object(pipeline, 'get_adapter', return_value=SimpleNamespace(
                fetch=lambda c: SimpleNamespace(path=str(source)))))
            stack.enter_context(patch.object(pipeline, 'extract', return_value=[
                Chapter(title='One', paragraphs=['Hello, my friend.'])]))
            def translated(**kwargs):
                kwargs['progress_cb'](1, 1)
                return TranslatedBook(title=candidate['title'], author='Author',
                    chapters=[TranslatedChapter(title='One', pairs=[
                        ('Hello, my friend.', '안녕, 나의 친구.')])],
                    quality_report={'passed': True})
            stack.enter_context(patch.object(pipeline, 'translate_book', side_effect=translated))
            stack.enter_context(patch.object(pipeline.cover_mod, 'find_cover', return_value=None))
            notify = stack.enter_context(patch.object(pipeline.search_mod, 'notify_line'))
            publish = stack.enter_context(patch.object(pipeline, 'publish_epub',
                                            return_value=(1, 'https://example.test/get/EPUB/1')))
            push = stack.enter_context(patch.object(pipeline, 'push_to_device', return_value=
                pipeline.PushResult(pushed=False, skipped=True, error='offline')))
            enqueue = stack.enter_context(patch.object(pipeline.pending_queue, 'enqueue'))
            req = catalog.new_request(candidate['title'])
            yield candidate, req, publish, push, enqueue, notify

    def test_precision_and_validated_artifact_complete_before_publish(self):
        with self.fixture() as (candidate, req, publish, push, enqueue, notify):
            def publish_checked(*args, **kwargs):
                record = catalog.get(req)
                self.assertEqual('validated', record['status'])
                self.assertTrue(record['quality_report']['passed'])
                self.assertTrue(record['epub_validation']['passed'])
                self.assertTrue(Path(record['epub_path']).is_file())
                self.assertEqual('en', kwargs['source_lang'])
                self.assertEqual('1', kwargs['source_id'])
                return 1, 'https://example.test/get/EPUB/1'
            publish.side_effect = publish_checked
            result = pipeline.run(candidate, req, notify=False)
            self.assertTrue(result['ok'])
            self.assertEqual('published', catalog.get(req)['status'])
            self.assertEqual(64, len(catalog.get(req)['artifact_sha256']))
            push.assert_not_called()
            enqueue.assert_not_called()
            notify.assert_not_called()

    def test_local_epub_does_not_require_calibre_or_device(self):
        with self.fixture() as (candidate, req, publish, push, enqueue, notify):
            result = pipeline.run(candidate, req, notify=False, publish_output=False)
            self.assertTrue(result['ok'])
            self.assertIsNone(result['opds_url'])
            self.assertEqual('completed', catalog.get(req)['status'])
            publish.assert_not_called()
            push.assert_not_called()
            enqueue.assert_not_called()
            notify.assert_not_called()

    def test_queue_failure_does_not_regress_publication(self):
        with self.fixture(device=True) as (candidate, req, publish, push, enqueue, notify):
            enqueue.side_effect = OSError('queue unavailable')
            result = pipeline.run(candidate, req, notify=False)
            self.assertTrue(result['ok'])
            self.assertEqual('published', catalog.get(req)['status'])
            self.assertIn('queue unavailable', result['warnings'][0])

    def test_notification_choice_is_saved_and_forwarded_to_retry_queue(self):
        for allowed in (False, True):
            with self.subTest(notify=allowed), self.fixture(device=True) as (candidate, req, publish, push, enqueue, notify):
                result = pipeline.run(candidate, req, notify=allowed, publish_output=False)
                self.assertTrue(result['ok'])
                self.assertIs(catalog.get(req)['notify'], allowed)
                self.assertIs(enqueue.call_args.kwargs['notify'], allowed)
                if not allowed:
                    notify.assert_not_called()

    def test_notification_exception_does_not_regress_publication(self):
        with self.fixture() as (candidate, req, publish, push, enqueue, notify):
            notify.side_effect = TimeoutError('notification unavailable')
            result = pipeline.run(candidate, req, notify=True)
            self.assertTrue(result['ok'])
            self.assertEqual('published', catalog.get(req)['status'])

    def test_final_status_read_failure_does_not_regress_publication(self):
        with self.fixture() as (candidate, req, publish, push, enqueue, notify):
            real_get = catalog.get

            def get_until_delivery_done(request_id):
                record = real_get(request_id)
                if record.get('status') == 'published' and 'device_push' in record:
                    raise OSError('status read unavailable')
                return record

            with patch.object(catalog, 'get', side_effect=get_until_delivery_done):
                result = pipeline.run(candidate, req, notify=False)
            self.assertTrue(result['ok'])
            self.assertIsNone(result['entry'])
            self.assertEqual('published', real_get(req)['status'])
            self.assertIn('final status read: status read unavailable', result['warnings'])

    def test_invalid_epub_never_reaches_publication(self):
        with self.fixture() as (candidate, req, publish, push, enqueue, notify):
            with patch.object(pipeline, 'validate_epub', side_effect=ValueError('broken anchor')):
                result = pipeline.run(candidate, req, notify=False)
            self.assertFalse(result['ok'])
            self.assertEqual('failed', catalog.get(req)['status'])
            publish.assert_not_called()
            push.assert_not_called()


if __name__ == '__main__':
    unittest.main()
