import unittest

from lxml import etree

from epub_markup import (EPUB_NS, XML_NS, plain_text, sanitize_inline,
                         stash_inline_tags, placeholders_preserved)


class InlineMarkupRegressions(unittest.TestCase):
    def test_prefixed_xhtml_and_epub_attributes_survive(self):
        value = ('<x:em xmlns:x="http://www.w3.org/1999/xhtml" '
                 'xmlns:ops="http://www.idpf.org/2007/ops" ops:type="noteref" '
                 'xml:lang="fr" class="voice">parole</x:em>')
        node = etree.fromstring(sanitize_inline(value).encode())
        self.assertEqual("em", node.tag)
        self.assertEqual("parole", node.text)
        self.assertEqual("fr", node.get(f"{{{XML_NS}}}lang"))
        self.assertEqual("noteref", node.get(f"{{{EPUB_NS}}}type"))
        self.assertEqual("voice", node.get("class"))
        self.assertEqual("parole", plain_text(value))

    def test_serialized_namespace_attributes_survive_second_sanitization(self):
        once = sanitize_inline('<a href="#note" epub:type="noteref">1</a>')
        twice = sanitize_inline(once)
        node = etree.fromstring(twice.encode())
        self.assertEqual("noteref", node.get(f"{{{EPUB_NS}}}type"))

    def test_namespace_declared_on_ancestor_is_resolved_before_cleanup(self):
        value = '<span xmlns:ops="http://www.idpf.org/2007/ops"><a ops:type="noteref">1</a></span>'
        root = etree.fromstring(sanitize_inline(value).encode())
        self.assertEqual("noteref", root[0].get(f"{{{EPUB_NS}}}type"))

    def test_entities_and_literal_comparisons_survive_when_xml_escaped(self):
        value = 'x&lt;y and a&gt;b. Use &lt;T&gt;. A &amp; B.'
        self.assertEqual("x<y and a>b. Use <T>. A & B.", plain_text(value))
        self.assertEqual(plain_text(value), plain_text(sanitize_inline(value)))

    def test_line_breaks_separate_words(self):
        self.assertEqual("free spirit", plain_text("free<br/>spirit"))

    def test_placeholder_duplicates_or_unknown_markers_are_rejected(self):
        value, mapping = stash_inline_tags('<em class="voice">voice</em>')
        self.assertTrue(placeholders_preserved(value, mapping))
        self.assertFalse(placeholders_preserved(value + next(iter(mapping)), mapping))
        self.assertFalse(placeholders_preserved(value + "◀OTHER_TAG7▶", mapping))

    def test_prefixed_active_content_is_removed(self):
        self.assertEqual("beforeafter", plain_text('before<x:script>evil()</x:script>after'))
        self.assertEqual("beforeafter", sanitize_inline('before<x:script>evil()</x:script>after'))


if __name__ == "__main__":
    unittest.main()
