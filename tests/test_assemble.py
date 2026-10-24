from __future__ import annotations

import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from lxml import etree

import assemble
import config
from epub_validate import EpubValidationError, validate_epub
from epub_markup import XHTML_NS, XML_NS

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6PwAAAABJRU5ErkJggg==')


def book(author='Test Author'):
    return {'title': 'A Portable Book', 'author': author, 'chapters': [
        {'title': 'Chapter One', 'pairs': [('A quiet river.', '고요한 강.')]}]}


class AssemblyTests(unittest.TestCase):
    def test_declares_both_languages_and_actual_cover_media(self):
        with tempfile.TemporaryDirectory() as d:
            report = {}
            path = assemble.assemble(book(), str(Path(d) / 'book.epub'),
                cover_bytes=PNG, source_lang='de', validation_report=report)
            with zipfile.ZipFile(path) as z:
                opf = etree.fromstring(z.read('OEBPS/content.opf'))
                ns = {'o': 'http://www.idpf.org/2007/opf', 'dc': 'http://purl.org/dc/elements/1.1/'}
                self.assertEqual(['de', 'ko'], opf.xpath('//dc:language/text()', namespaces=ns))
                self.assertEqual(['image/png'], opf.xpath('//o:item[@id="cover-image"]/@media-type', namespaces=ns))
            self.assertTrue(report['passed'])

    def test_translated_inline_markup_inherits_korean_language(self):
        source = '<em lang="fr" xml:lang="fr">Bonjour</em>'
        output = '<em lang="fr" xml:lang="fr">안녕하세요</em>'
        xml = assemble._build_chapter_xhtml('Title', [(source, output)])
        root = etree.fromstring(xml.encode())
        ns = {'x': XHTML_NS}
        original = root.xpath('//x:span[@class="bf-original"]/x:em', namespaces=ns)[0]
        translated = root.xpath('//x:span[@class="bf-translation"]/x:em', namespaces=ns)[0]
        self.assertEqual('fr', original.get('lang'))
        self.assertIsNone(translated.get('lang'))
        self.assertIsNone(translated.get(f'{{{XML_NS}}}lang'))
        self.assertEqual('ko', translated.getparent().get('lang'))

    def test_same_title_different_authors_never_overwrite(self):
        with tempfile.TemporaryDirectory() as d, patch.object(config, 'LIBRARY_DIR', d):
            first = assemble.assemble(book('Author One'))
            original = Path(first).read_bytes()
            second = assemble.assemble(book('Author Two'))
            self.assertNotEqual(first, second)
            self.assertEqual(original, Path(first).read_bytes())

    def test_failed_validation_preserves_previous_output(self):
        with tempfile.TemporaryDirectory() as d:
            destination = Path(d) / 'book.epub'
            destination.write_bytes(b'previous valid edition')
            with patch('epub_validate.validate_epub', side_effect=EpubValidationError('fixture failure')):
                with self.assertRaises(EpubValidationError):
                    assemble.assemble(book(), str(destination))
            self.assertEqual(b'previous valid edition', destination.read_bytes())
            self.assertEqual([destination], list(Path(d).iterdir()))

    def test_blank_or_empty_books_are_rejected(self):
        for candidate in [
            {'title': 'Empty', 'chapters': []},
            {'title': 'Empty', 'chapters': [{'title': '', 'pairs': []}]},
            {'title': 'Blank', 'chapters': [{'title': '', 'pairs': [('Text', '')]}]},
        ]:
            with self.assertRaises(ValueError):
                assemble.assemble(candidate)

    def test_packaged_svg_cannot_bypass_active_content_checks(self):
        for svg in [b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
                    b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>']:
            candidate=book();candidate['chapters'][0]['assets']={'assets/figure.svg':svg}
            with tempfile.TemporaryDirectory() as directory:
                destination=Path(directory)/'book.epub'
                with self.assertRaisesRegex(EpubValidationError,'active content|event handler'):
                    assemble.assemble(candidate,str(destination))
                self.assertFalse(destination.exists())

    def test_broken_anchor_is_rejected_before_replace(self):
        candidate = book()
        candidate['chapters'][0]['pairs'] = [('<a href="#missing">A river.</a>', '<a href="#missing">강.</a>')]
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'book.epub'
            with self.assertRaisesRegex(EpubValidationError, 'broken anchor'):
                assemble.assemble(candidate, str(target))
            self.assertFalse(target.exists())

    def test_bilingual_navigation_retains_previous_feature(self):
        nav = assemble._build_nav_xhtml('Book', [(1, 'River')], ['강'])
        ncx = assemble._build_toc_ncx('id', ['River'], ['강'], book_title='Book')
        self.assertIn('River (강)', ''.join(etree.fromstring(nav.encode()).itertext()))
        self.assertIn('River (강)', ncx)
        self.assertIn('<docTitle><text>Book</text></docTitle>', ncx)

    def test_template_cannot_drop_or_reorder_translation_units(self):
        template = f'<body xmlns="{XHTML_NS}"><p data-bf-unit="1">A quiet river.</p></body>'
        with self.assertRaisesRegex(ValueError, 'alignment mismatch'):
            assemble._build_chapter_xhtml('Title', [('A quiet river.', '고요한 강.')], template=template)

    def test_required_external_checker_cannot_silently_skip(self):
        with tempfile.TemporaryDirectory() as d:
            path = assemble.assemble(book(), str(Path(d)/'book.epub'))
            with patch.object(config, 'DATA_DIR', d), patch('epub_validate.shutil.which', return_value=None), \
                    patch.dict('os.environ', {'BABELFORGE_REQUIRE_EPUBCHECK':'1', 'EPUBCHECK_JAR':''}):
                with self.assertRaisesRegex(EpubValidationError, 'required'):
                    validate_epub(path)


if __name__ == '__main__':
    unittest.main()
