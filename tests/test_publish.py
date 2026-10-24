from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import publish


class PublishTests(unittest.TestCase):
    def test_calibre_acquisition_url_uses_live_get_route(self):
        with patch('publish.config.OPDS_BASE_URL', 'https://books.example'):
            self.assertEqual('https://books.example/get/EPUB/33', publish.opds_url_for(33))

    def test_existing_edition_keeps_id_and_never_deletes(self):
        with tempfile.TemporaryDirectory() as directory:
            epub = os.path.join(directory, 'book.epub')
            Path(epub).write_bytes(b'fixture epub')
            identifier = publish.edition_identifier(epub, 'gutenberg', '1', 'de')
            listing = json.dumps([{'id': 42, 'identifiers': {'babelforge': identifier}}])
            with patch.object(publish.config, 'DATA_DIR', directory), patch.object(publish, '_run', side_effect=[listing, '', '']) as run:
                self.assertEqual(42, publish.add_to_library(epub, 'Title', 'Author', 'gutenberg', source_id='1', source_lang='de'))
            commands = [call.args[0] for call in run.call_args_list]
            self.assertTrue(any('add_format' in argv for argv in commands))
            self.assertFalse(any('remove' in argv or 'add' in argv for argv in commands))
            self.assertIn('languages:ko,de', commands[-1])

    def test_failed_replacement_never_removes_existing_book(self):
        with tempfile.TemporaryDirectory() as directory:
            epub = os.path.join(directory, 'book.epub')
            Path(epub).touch()
            with patch.object(publish.config, 'DATA_DIR', directory), patch.object(publish, '_matching_ids', return_value=[42]), patch.object(publish, '_run', side_effect=RuntimeError('replacement rejected')) as run:
                with self.assertRaises(RuntimeError):
                    publish.add_to_library(epub, 'Title', 'Author')
            self.assertEqual(1, run.call_count)
            self.assertIn('add_format', run.call_args.args[0])
            self.assertNotIn('remove', run.call_args.args[0])

    def test_new_edition_resolves_own_identifier_not_latest_book(self):
        with tempfile.TemporaryDirectory() as directory:
            epub = os.path.join(directory, 'book.epub')
            Path(epub).touch()
            identifier = publish.edition_identifier(epub, 'gutenberg', '2', 'ja')
            final = json.dumps([{'id': 17, 'identifiers': {'babelforge': identifier}},
                                {'id': 99, 'identifiers': {'babelforge': 'unrelated'}}])
            with patch.object(publish.config, 'DATA_DIR', directory), patch.object(publish, '_run', side_effect=['[]', 'localized output', final]) as run:
                self.assertEqual(17, publish.add_to_library(epub, 'Same title', 'Author', 'gutenberg', source_id='2', source_lang='ja'))
            args = run.call_args_list[1].args[0]
            self.assertIn('--duplicates', args)
            self.assertIn('ko,ja', args)
            self.assertIn('babelforge:' + identifier, args)

    def test_failed_lookup_does_not_add_or_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            epub = os.path.join(directory, 'book.epub')
            Path(epub).touch()
            with patch.object(publish.config, 'DATA_DIR', directory), patch.object(publish, '_run', return_value='not JSON') as run:
                with self.assertRaises(ValueError):
                    publish.add_to_library(epub, 'Title', 'Author')
            self.assertEqual(1, run.call_count)

    def test_identity_changes_with_edition_not_translation_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            epub = os.path.join(directory, 'book.epub')
            Path(epub).write_bytes(b'old translation')
            first = publish.edition_identifier(epub, 'gutenberg', '1', 'de')
            Path(epub).write_bytes(b'new translation')
            self.assertEqual(first, publish.edition_identifier(epub, 'gutenberg', '1', 'de'))
            self.assertNotEqual(first, publish.edition_identifier(epub, 'gutenberg', '2', 'de'))
