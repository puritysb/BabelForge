from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import config
from extract import _extract_txt


class TextExtractionTests(unittest.TestCase):
    def test_forced_budget_split_keeps_honorific_with_name(self):
        from extract import _bounded_text_chunks
        for title in ("Mr.", "Mrs.", "Dr."):
            for sentence_split in (False, True):
                with self.subTest(title=title, sentence_split=sentence_split):
                    budget = 45
                    text = ("x" * (budget - len(title) - 2) + " " + title
                            + " Bennet spoke. " + "Further words " * 8)
                    chunks = _bounded_text_chunks(text, budget, sentence_split)
                    self.assertEqual(text, "".join(chunks))
                    self.assertTrue(all(len(part) <= budget for part in chunks))
                    self.assertFalse(any(part.rstrip().endswith(title) for part in chunks))
                    self.assertTrue(any(title + " Bennet" in part for part in chunks))

    def test_indented_german_contents_restores_real_chapters(self):
        first = "\n\n".join(
            f"Vorrede Absatz {index} mit genügend Text."
            for index in range(1, 10))
        second = "\n\n".join(
            f"Philosophischer Absatz {index} mit genügend Text."
            for index in range(1, 10))
        source = f"""*** START OF THE PROJECT GUTENBERG EBOOK TEST ***

Inhalt

    An Stelle einer Vorrede
    Von den ersten und letzten Dingen
    Ein Nachspiel

An Stelle einer Vorrede.

{first}

Von den ersten und letzten Dingen.

{second}

Ein Nachspiel.

Letzter Absatz.

*** END OF THE PROJECT GUTENBERG EBOOK TEST ***
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "book.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source)
            with patch.object(config, "SENTENCE_LEVEL_SPLIT", False):
                chapters = _extract_txt(path)

        self.assertEqual([
            "An Stelle einer Vorrede.",
            "Von den ersten und letzten Dingen.",
            "Ein Nachspiel.",
        ], [chapter.title for chapter in chapters])
        self.assertEqual(
            "An Stelle einer Vorrede.", chapters[0].paragraphs[0])
        self.assertNotIn("Untitled", " ".join(ch.title for ch in chapters))


class SourcePreservationTests(unittest.TestCase):
    def _text(self, raw):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "book.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(raw)
            with patch.object(config, "SENTENCE_LEVEL_SPLIT", False):
                return _extract_txt(path)

    def test_substantive_introduction_is_preserved(self):
        chapters = self._text("A substantive introduction.\n\nCHAPTER I\n\nBody text.")
        self.assertEqual(["Preface", "CHAPTER I"], [ch.title for ch in chapters])
        self.assertEqual(["A substantive introduction."], chapters[0].paragraphs)

    def test_short_chapters_are_not_merged_and_single_character_is_kept(self):
        chapters = self._text("CHAPTER I\n\nI\n\nCHAPTER II\n\nSecond body.\n\nCHAPTER III\n\nThird body.")
        self.assertEqual(["CHAPTER I", "CHAPTER II", "CHAPTER III"], [ch.title for ch in chapters])
        self.assertEqual(["CHAPTER I", "I"], chapters[0].paragraphs)

    def test_short_cjk_sentences_are_not_deleted(self):
        from extract import split_sentences
        with patch.object(config, "SENTENCE_LEVEL_SPLIT", True):
            self.assertEqual(["え？", "彼は戻った。"], split_sentences("え？ 彼は戻った。"))

    def test_actual_txt_headings_are_translated_and_not_duplicated(self):
        from lxml import etree
        chapters = self._text("Introduction.\n\nCHAPTER I\n\nBody.\n\nCHAPTER II")
        self.assertEqual(["Introduction."], chapters[0].paragraphs)
        self.assertEqual(["CHAPTER I", "Body."], chapters[1].paragraphs)
        self.assertEqual(["CHAPTER II"], chapters[2].paragraphs)
        heading = etree.fromstring(chapters[1].template.encode()).xpath(".//*[local-name()='h1']")[0]
        self.assertEqual("0", heading.get("data-bf-unit"))
        synthetic = etree.fromstring(chapters[0].template.encode()).xpath(".//*[local-name()='h1']")[0]
        self.assertIsNone(synthetic.get("data-bf-unit"))

    def test_txt_literals_are_escaped_as_inline_xhtml(self):
        from epub_markup import plain_text, sanitize_inline
        raw = "x<y and a>b. Use <T> as a parameter. R&D &lt; stays literal."
        chapters = self._text(raw)
        fragment = chapters[0].paragraphs[0]
        self.assertEqual(raw, plain_text(fragment))
        self.assertEqual(raw, plain_text(sanitize_inline(fragment)))
        self.assertIn("&lt;T&gt;", fragment)
        self.assertIn("&amp;lt;", fragment)

    def test_epub_spine_structure_images_and_linked_nonspine_footnotes(self):
        from ebooklib import epub
        from bs4 import BeautifulSoup
        from lxml import etree
        from extract import _extract_epub
        book = epub.EpubBook()
        book.set_identifier("source-preservation")
        book.set_title("Fixture book")
        book.set_language("en")
        first = epub.EpubHtml(uid="first", title="First", file_name="text/first.xhtml")
        first.content = ('<h1>First</h1><p id="entry">A <em>significant</em> sentence. '
                         '<a epub:type="noteref" href="../notes.xhtml#note">1</a></p>'
                         '<div class="poem"><div>Poem line A</div><div>Poem line B</div></div>'
                         '<ol><li><p>Nested list paragraph.</p></li></ol>'
                         '<table><tr><th>Heading cell</th><td>Table text</td></tr></table>'
                         '<p>Before image.<img src="../images/picture.png" alt="Illustration"/>After image.</p>'
                         '<h2>Subsection</h2><script>UNSAFE SCRIPT</script>')
        second = epub.EpubHtml(uid="second", title="Second", file_name="text/second.xhtml")
        second.content = '<h1>Second</h1><p>Second body.</p>'
        notes = epub.EpubHtml(uid="notes", title="Notes", file_name="notes.xhtml")
        notes.content = '<aside epub:type="footnote" id="note"><p>Footnote content. <a href="text/first.xhtml#entry">Back</a></p></aside>'
        book.add_item(second)
        book.add_item(first)
        book.add_item(notes)
        book.add_item(epub.EpubNav())
        book.add_item(epub.EpubItem(uid="image", file_name="images/picture.png", media_type="image/png", content=b"\x89PNG\r\n\x1a\nfixture"))
        book.toc = [first, second]
        # A real EPUB can list its nav document in the reading spine.
        # EbookLib reads that item's properties as [] and its type as EpubNav.
        book.spine = ["nav", first, second]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "book.epub")
            epub.write_epub(path, book)
            chapters = _extract_epub(path)
        self.assertEqual(["First", "Second", "notes"], [ch.title for ch in chapters])
        units = [BeautifulSoup(unit, "html.parser").get_text() for ch in chapters for unit in ch.paragraphs]
        all_text = "\n".join(units)
        for expected in ("Poem line A", "Poem line B", "Nested list paragraph.", "Heading cell", "Table text", "Subsection", "Footnote content.", "Before image.", "After image."):
            self.assertEqual(1, all_text.count(expected), expected)
        self.assertNotIn("UNSAFE SCRIPT", all_text)
        self.assertIn("<em", "".join(chapters[0].paragraphs))
        first_body = etree.fromstring(chapters[0].template.encode())
        self.assertTrue(first_body.xpath(".//*[local-name()='a' and @href='chapter3.xhtml#note']"))
        self.assertTrue(etree.fromstring(chapters[2].template.encode()).xpath(".//*[local-name()='a' and @href='chapter1.xhtml#entry']"))
        images = first_body.xpath(".//*[local-name()='img']")
        self.assertEqual(1, len(images))
        self.assertIn(images[0].get("src"), chapters[0].assets)
        self.assertFalse(images[0].xpath("ancestor::*[@data-bf-unit]"))
        for chapter in chapters:
            body = etree.fromstring(chapter.template.encode())
            marked = body.xpath(".//*[@data-bf-unit]")
            self.assertEqual(list(range(len(chapter.paragraphs))), [int(node.get("data-bf-unit")) for node in marked])
            self.assertFalse(body.xpath(".//*[@data-bf-unit]//*[@data-bf-unit]"))

    def test_mixed_container_direct_text_is_partitioned_once(self):
        from extract import _document_body, _mark_units
        from bs4 import BeautifulSoup
        body = _document_body(b'<html xmlns="http://www.w3.org/1999/xhtml"><body><div>Before <em>one</em><p>Inside paragraph</p>After <b>two</b></div></body></html>')
        units = _mark_units(body)
        self.assertEqual(["Before one", "Inside paragraph", "After two"], [BeautifulSoup(unit, "html.parser").get_text() for unit in units])

    def test_comments_do_not_delete_the_following_source_text(self):
        from extract import _document_body, _mark_units
        body = _document_body(b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Before<!-- editorial comment -->after</p></body></html>')
        self.assertEqual(["Beforeafter"], _mark_units(body))

    def test_invalid_txt_encoding_is_rejected_instead_of_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "book.txt")
            with open(path, "wb") as handle:
                handle.write(b"Invalid text \xff source")
            with self.assertRaises(UnicodeDecodeError):
                _extract_txt(path)

    def test_html_language_prefixes_produce_valid_xhtml(self):
        from extract import _document_body, _mark_units
        from lxml import etree
        body = _document_body(b'<html><body><p xml:lang="fr">Bonjour.</p></body></html>', html=True)
        self.assertEqual(["Bonjour."], _mark_units(body))
        parsed = etree.fromstring(etree.tostring(body))
        paragraph = parsed.xpath(".//*[local-name()='p']")[0]
        self.assertEqual("fr", paragraph.get("{http://www.w3.org/XML/1998/namespace}lang"))

    def test_long_inline_block_is_bounded_without_losing_text_or_wrappers(self):
        from extract import _document_body, _mark_units
        from lxml import etree
        text = ('<html xmlns="http://www.w3.org/1999/xhtml"><body><p id="paragraph">'
                'Leading words. <em id="emphasis">' + 'An emphasized sentence &amp; more. ' * 20 + '</em>'
                ' Between <a href="#note">' + 'A long linked sentence. ' * 15 + '</a> trailing words.</p>'
                '<p id="note">Note.</p></body></html>')
        body = _document_body(text.encode())
        before = "".join(body.itertext())
        with patch.object(config, "TRANSLATE_BATCH_MAX_CHARS", 80):
            units = _mark_units(body)
        self.assertGreater(len(units), 10)
        self.assertTrue(all(len(unit) <= 80 for unit in units))
        self.assertEqual(before, "".join(body.itertext()))
        self.assertEqual(2, len(body.xpath(".//*[local-name()='p']")))
        self.assertEqual(1, len(body.xpath(".//*[local-name()='em' and @id='emphasis']")))
        self.assertEqual(1, len(body.xpath(".//*[local-name()='a' and @href='#note']")))
        self.assertFalse(body.xpath(".//*[@data-bf-unit]//*[@data-bf-unit]"))
        etree.fromstring(etree.tostring(body))

    def test_long_txt_keeps_one_paragraph_with_bounded_chunks(self):
        from lxml import etree
        from html import unescape
        raw = "A sentence with <T> & symbols. " * 30 + "終" * 170
        with patch.object(config, "TRANSLATE_BATCH_MAX_CHARS", 80):
            chapters = self._text(raw)
        self.assertTrue(all(len(unit) <= 80 for unit in chapters[0].paragraphs))
        self.assertGreater(len(chapters[0].paragraphs), 10)
        self.assertEqual(raw, "".join(unescape(unit) for unit in chapters[0].paragraphs))
        body = etree.fromstring(chapters[0].template.encode())
        paragraphs = body.xpath(".//*[local-name()='p']")
        self.assertEqual(1, len(paragraphs))
        self.assertEqual(raw, "".join(paragraphs[0].itertext()))

    def test_sentence_mode_preserves_spaces_inside_original_paragraph(self):
        from extract import _document_body, _mark_units
        body = _document_body(b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>First. Second! Third?</p></body></html>')
        units = _mark_units(body, sentence_split=True)
        self.assertEqual(["First. ", "Second! ", "Third?"], units)
        self.assertEqual("First. Second! Third?", "".join(body.itertext()))

    def test_plain_html_is_extracted_without_markup_as_source_text(self):
        from extract import extract
        from sources import Candidate, SourceText
        from bs4 import BeautifulSoup
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "book.html")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('<html><body><h1>Title</h1><p>A <em>real</em> paragraph.</p></body></html>')
            chapters = extract(SourceText(Candidate("gutenberg", "1", "Title", "Author"), "html", path))
        self.assertEqual(["Title", "A real paragraph."], [BeautifulSoup(unit, "html.parser").get_text() for unit in chapters[0].paragraphs])
        self.assertIsNotNone(chapters[0].template)

    def test_html_with_unavailable_media_fails_before_translation(self):
        from extract import extract
        from sources import Candidate, SourceText
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "book.html")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('<html><body><p>Text</p><img src="picture.jpg"/></body></html>')
            with self.assertRaisesRegex(ValueError, "media"):
                extract(SourceText(Candidate("gutenberg", "1", "Title", "Author"), "html", path))


class PortableEpubIntegrationTests(unittest.TestCase):
    def test_extracted_structure_survives_bilingual_assembly(self):
        import base64
        import zipfile
        from ebooklib import epub
        from lxml import etree
        from extract import _extract_epub
        from assemble import assemble
        from epub_validate import validate_epub
        source = epub.EpubBook()
        source.set_identifier("structure-roundtrip")
        source.set_title("Structure roundtrip")
        source.set_language("en")
        cover = epub.EpubHtml(uid="cover", title="Cover", file_name="cover.xhtml")
        cover.content = '<div><img src="images/pixel.png" alt="Cover"/></div>'
        first = epub.EpubHtml(uid="first", title="First", file_name="text/first.xhtml")
        first.content = ('<h1>First</h1><p id="intro">A &lt;literal&gt; &amp; '
                         '<em id="emphasis">important</em> source '
                         '<a epub:type="noteref" href="../notes.xhtml#n1">1</a>.</p>'
                         '<div class="poem"><div>Verse one.</div><div>Verse two.</div></div>'
                         '<table><tr><th>Column</th><td>Value</td></tr></table>'
                         '<img src="../images/diagram.svg" alt="Vector illustration"/>'
                         '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><title>Diagram</title><circle cx="5" cy="5" r="4"/></svg>'
                         '<math xmlns="http://www.w3.org/1998/Math/MathML" alttext="x"><mi>x</mi></math>')
        first.content += ('<p id="long">Leading <em id="long-emphasis">' +
                          'A long source sentence with meaning. ' * 120 +
                          '</em> Ending.</p>')
        notes = epub.EpubHtml(uid="notes", title="Notes", file_name="notes.xhtml")
        notes.content = '<aside epub:type="footnote" id="n1"><p>Footnote. <a href="text/first.xhtml#intro">Return</a></p></aside>'
        for item in (notes, first, cover, epub.EpubNav()):
            source.add_item(item)
        source.add_item(epub.EpubItem(uid="pixel", file_name="images/pixel.png", media_type="image/png", content=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=")))
        source.add_item(epub.EpubItem(uid="diagram", file_name="images/diagram.svg", media_type="image/svg+xml",
                                     content=b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 10 10"><title>Diagram</title><circle id="shape" cx="5" cy="5" r="2"/><use xlink:href="#shape"/><image xlink:href="pixel.png" width="10" height="10"/></svg>'))
        source.spine = [cover, first]
        source.toc = [first]
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "source.epub")
            epub.write_epub(source_path, source)
            chapters = _extract_epub(source_path)
            output = assemble({"title": "Structure roundtrip", "author": "Fixture",
                               "chapters": [{"title": ch.title, "pairs": [(unit, unit + " 한국어") for unit in ch.paragraphs],
                                             "template": ch.template, "assets": ch.assets} for ch in chapters]},
                              out_path=os.path.join(directory, "result.epub"))
            validate_epub(output, expected_pairs=sum(len(ch.paragraphs) for ch in chapters))
            with zipfile.ZipFile(output) as archive:
                cover_body = etree.fromstring(archive.read("OEBPS/chapter1.xhtml"))
                body = etree.fromstring(archive.read("OEBPS/chapter2.xhtml"))
                footnotes = etree.fromstring(archive.read("OEBPS/chapter3.xhtml"))
                self.assertEqual(1, len(cover_body.xpath("//*[local-name()='img']")))
                self.assertEqual(1, len(body.xpath("//*[local-name()='table']")))
                self.assertEqual(1, len(body.xpath("//*[@id='emphasis']")))
                self.assertEqual(1, len(body.xpath("//*[local-name()='p' and @id='long']")))
                self.assertEqual(1, len(body.xpath("//*[local-name()='em' and @id='long-emphasis']")))
                self.assertGreater(len(body.xpath("//*[@id='long-emphasis']//*[@class=$kind]", kind=config.ORIGINAL_CLASS)), 1)
                self.assertEqual(1, len(footnotes.xpath("//*[@id='n1']")))
                self.assertEqual(2, len(body.xpath("//*[local-name()='a' and @href='chapter3.xhtml#n1']")))
                self.assertIn("<literal>", "".join(body.itertext()))
                self.assertNotIn("data-bf-unit", archive.read("OEBPS/chapter2.xhtml").decode())
                self.assertEqual(1, len(body.xpath("//*[local-name()='svg']")))
                self.assertEqual(1, len(body.xpath("//*[local-name()='math']")))
                opf = etree.fromstring(archive.read("OEBPS/content.opf"))
                item = opf.xpath("//*[local-name()='item' and @href='chapter2.xhtml']")[0]
                self.assertEqual({"svg", "mathml"}, set(item.get("properties").split()))
                svg_name = next(name for name in archive.namelist() if name.endswith("-diagram.svg"))
                vector = etree.fromstring(archive.read(svg_name))
                image = vector.xpath("//*[local-name()='image']")[0]
                raster_href = image.get("{http://www.w3.org/1999/xlink}href")
                self.assertIn("OEBPS/assets/" + raster_href, archive.namelist())


if __name__ == "__main__":
    unittest.main()
