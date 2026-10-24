"""Existing-edition coverage recovery must never fabricate alignments."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import assemble
from epub_markup import plain_text
from extract import _extract_txt
from review_library import read_bilingual, rebase_txt, normalized
from translate import TranslatedBook, TranslatedChapter


class LegacyReviewTests(unittest.TestCase):
    def test_missing_dialogue_and_number_are_restored_with_blank_korean(self):
        old = TranslatedBook("Test", "Author", [TranslatedChapter("Old safety chunk", [
            ("He spoke.", "그가 말했다."), ("She left.", "그녀가 떠났다.")])])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.txt"
            path.write_text('CHAPTER I\n\n1\n\nHe spoke.\n\n"Stay!" she cried.\n\nShe left.')
            result, coverage = rebase_txt(old, path)
        pairs = [p for c in result.chapters for p in c.pairs]
        self.assertEqual("CHAPTER I", result.chapters[0].title)
        self.assertIn(("He spoke.", "그가 말했다."), pairs)
        self.assertIn(("She left.", "그녀가 떠났다."), pairs)
        self.assertIn(("1", ""), pairs)
        self.assertTrue(any(plain_text(s) == '"Stay!" she cried.' and ko == "" for s, ko in pairs))
        self.assertEqual(2, coverage["reused_pairs"])
        self.assertTrue(coverage["source_coverage_verified"])

    def test_other_edition_fails_without_approximate_source_matching(self):
        old = TranslatedBook("Test", "Author", [TranslatedChapter("I", [("He went home.", "그는 집으로 갔다.")])])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.txt"
            path.write_text("He walked home.")
            with self.assertRaisesRegex(ValueError, "edition/alignment mismatch"):
                rebase_txt(old, path)

    def test_legacy_sentence_boundaries_remain_inside_original_paragraph(self):
        old = TranslatedBook("Test", "Author", [TranslatedChapter("I", [
            ("He spoke.", "그가 말했다."), ("She left.", "그녀가 떠났다.")])])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.txt"
            path.write_text("He spoke. She left.")
            result, _ = rebase_txt(old, path)
        self.assertEqual(2, len(result.chapters[0].pairs))
        self.assertEqual(normalized("He spoke. She left."), "".join(
            normalized(plain_text(s)) for s, _ in result.chapters[0].pairs))
        self.assertEqual(1, result.chapters[0].template.count('<p>'))

    def test_wrapped_gutenberg_credits_and_end_marker_leave_no_asterisk(self):
        raw = '''*** START OF THE PROJECT GUTENBERG EBOOK TEST ***

Produced by Gary and the Online
Distributed Proofreading Team at https://www.pgdp.net

CHAPTER I

Actual text.

*** END OF THE PROJECT GUTENBERG EBOOK TEST ***
License text.
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "book.txt"
            path.write_text(raw)
            result = _extract_txt(str(path))
        content = [plain_text(s) for c in result for s in c.paragraphs]
        self.assertEqual(["CHAPTER I", "Actual text."], content)

    def test_missing_required_lexical_distinction_is_repaired_not_silently_dropped(self):
        import precision
        from extract import Chapter
        first = {"source_terms": ["free spirits"], "canonical_ko": "자유정신", "confidence": "high"}
        rare = {"source_terms": ["free-thinkers"], "canonical_ko": "자유사상가", "confidence": "high"}
        responses = iter([{"concepts": [first, rare]}, {"concepts": [first, rare]},
                          {"concepts": [first]}, {"concepts": [first, rare]}])
        calls = []
        def chat(messages):
            calls.append(messages)
            return json.dumps(next(responses))
        with patch("precision._draft_concepts", return_value=[first]), \
                patch("precision._vet_related_candidates", return_value=[rare]), \
                patch("precision._web_evidence", return_value=([], set())):
            sheet = precision.build_concept_sheet([Chapter("I", ["free spirits and free-thinkers"] )],
                "Test", "Author", "en", {}, chat, lambda *a, **k: [])
        self.assertEqual(4, len(calls))
        self.assertEqual("자유사상가", precision.flat_glossary(sheet)["free-thinkers"])

    def test_final_formatting_restores_source_verse_and_missing_chapter_navigation(self):
        import zipfile
        from lxml import etree
        from finish_library_review import refine
        pairs = [("_Chapter I._", "제1장"),
                 ("Roses are red. Violets are blue. Sugar is sweet.", "장미는 붉고 제비꽃은 푸르고 설탕은 달다.")]
        book = {"title": "Test", "chapters": [{"title": "Preface", "pairs": pairs}]}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.txt"
            source.write_text("_Chapter I._\n\n    Roses are red.\n    Violets are blue.\n    Sugar is sweet.")
            path = Path(assemble.assemble(book, str(Path(directory) / "book.epub")))
            formatting, report = refine(path, {"source_path": str(source), "source_format": "txt"}, pairs)
            self.assertTrue(report["content_compared"])
            self.assertEqual(1, formatting["source_blocks_with_restored_linebreaks"])
            self.assertEqual(1, formatting["added_chapter_navigation_entries"])
            with zipfile.ZipFile(path) as archive:
                nav = etree.fromstring(archive.read("OEBPS/nav.xhtml"))
                self.assertEqual(2, len(nav.xpath('//*[local-name()="a"]')))
                chapter = etree.fromstring(archive.read("OEBPS/chapter1.xhtml"))
                original = chapter.xpath('//*[@class="bf-original"]')[1]
                self.assertEqual(2, original.text.count("\n"))
                translated = original.getnext()
                self.assertEqual(pairs[1][1], "".join(translated.itertext()))

    def test_pipeline_debris_cannot_be_kept_or_inherited_from_old_approval(self):
        from quality_audit import QualityAuditError, audit_translation_quality, find_translation_integrity_violations
        for token in ("⟦P⟔", "⟦P⟧", "◀Q0TAG1▶"):
            with self.subTest(token=token):
                book = TranslatedBook("Test", "Author", [TranslatedChapter("I", [
                    ("One sentence.", "한 문장이다. " + token)])])
                self.assertTrue(find_translation_integrity_violations(book))
                def keep(_):
                    return json.dumps({"paragraphs": [{"chapter_index": 0,
                        "paragraph_index": 0, "action": "keep"}]})
                with self.assertRaises(QualityAuditError):
                    audit_translation_quality(book, keep)
        literal = TranslatedBook("Test", "Author", [TranslatedChapter("I", [
            ("The marker is ⟦P⟧.", "표지는 ⟦P⟧이다.")])])
        self.assertFalse(find_translation_integrity_violations(literal))

    def test_final_quote_gate_finds_lost_closing_mark_after_correcting_negation(self):
        from finish_library_review import quotation_losses
        source = 'Must we not be deceivers too?" Such thoughts lead him onward.'
        before = '우리는 속이는 자가 아니어야 하는가?" 이런 생각이 그를 이끈다.'
        after = '우리도 속이는 자여야 하지 않는가? 이런 생각이 그를 이끈다.'
        baseline = {"chapters": [{"pairs": [(source, before)]}]}
        book = TranslatedBook("Test", "Author", [TranslatedChapter("I", [(source, after)])])
        self.assertEqual(1, len(quotation_losses(book, baseline)))
        # Restore the closing mark, preserving every word.
        book.chapters[0].pairs[0] = (source, after.replace('는가? 이런', '는가?" 이런'))
        self.assertEqual([], quotation_losses(book, baseline))

    def test_author_initials_are_preserved_but_short_prose_still_requires_korean(self):
        from quality_audit import QualityAuditError, audit_translation_quality
        def keep(_):
            return json.dumps({"paragraphs": [{"chapter_index": 0,
                "paragraph_index": 0, "action": "keep"}]})
        for text in ("J. M.", "J.M.", "F.R.S."):
            book = TranslatedBook("Test", "Author", [TranslatedChapter("I", [(text, text)])])
            audit_translation_quality(book, keep)
        book = TranslatedBook("Test", "Author", [TranslatedChapter("I", [("No!", "No!")])])
        with self.assertRaises(QualityAuditError):
            audit_translation_quality(book, keep)

    def test_short_japanese_name_and_full_name_survive_cross_script_metadata(self):
        from precision import _validate_concepts
        concepts = _validate_concepts([{
            "source_terms": ["八木独仙", "独仙"], "original_terms": ["Yagi Dokusen"],
            "canonical_ko": "도쿠센", "strict": True}], "八木独仙は独仙と呼ばれる。", set())
        self.assertEqual(["八木独仙", "独仙"], concepts[0]["source_terms"])
        self.assertEqual(["Yagi Dokusen"], concepts[0]["original_terms"])

    def test_numeric_label_can_ground_removal_of_duplicated_following_paragraph(self):
        from quality_audit import audit_translation_quality
        book = TranslatedBook("Test", "Author", [TranslatedChapter("I", [
            ("7.", "다음 구간의 번역을 잘못 복제했다.")])])
        responses = iter([{"paragraphs": [{"chapter_index": 0, "paragraph_index": 0,
            "action": "correct", "corrected_ko": "7.", "category": "meaning",
            "reason": "Only the section number is in this source unit.", "source_quote": "7."}]},
            {"paragraphs": [{"chapter_index": 0, "paragraph_index": 0, "action": "keep"}]}])
        audit_translation_quality(book, lambda _: json.dumps(next(responses)))
        self.assertEqual("7.", book.chapters[0].pairs[0][1])
        self.assertEqual([False, True], [row["approved"] for row in book.audit_log])

    def test_numbered_label_with_a_long_copied_paragraph_cannot_be_kept(self):
        from quality_audit import QualityAuditError, audit_translation_quality
        copied = "다음 절의 번역을 이 번호 칸에 잘못 복사하여 본문이 중복된 상태이다. " * 3
        book = TranslatedBook("Test", "Author", [TranslatedChapter("I", [("7.", copied)])])
        def keep(_):
            return json.dumps({"paragraphs": [{"chapter_index": 0,
                "paragraph_index": 0, "action": "keep"}]})
        with self.assertRaises(QualityAuditError):
            audit_translation_quality(book, keep)
        label = TranslatedBook("Test", "Author", [TranslatedChapter("I", [("3899.", "삼천팔백구십구 번째 절")])])
        audit_translation_quality(label, keep)

    def test_repeated_legitimate_avoid_words_use_explicit_occurrence_ids(self):
        from quality_audit import audit_and_correct, QualityAuditError
        source = "Cultur und Bildung (welche immer Bildbarkeit voraussetzt)."
        korean = "문화와 교양(교양은 언제나 교양 가능성을 전제한다)."
        sheet = {"concepts": [{"concept_id": "culture", "source_terms": ["Cultur"],
            "canonical_ko": "문화", "avoid_ko": ["교양"], "strict": True}]}
        def review(messages):
            row = json.loads(messages[1]["content"])["paragraphs"][0]
            occurrences = row["violations"][0]["avoid_ko_occurrences"]
            self.assertEqual([1, 2, 3], [r["occurrence"] for r in occurrences])
            exemptions = [{"concept_id": "culture", "avoid_ko": "교양",
                "source_expression": expression, "ko_occurrence": i,
                "reason": "personal cultivation/anaphor/educability, not civilization"}
                for i, expression in enumerate(["Bildung", "welche", "Bildbarkeit"], 1)]
            return json.dumps({"paragraphs": [{"chapter_index": 0, "paragraph_index": 0,
                "action": "keep", "corrected_ko": korean, "contextual_exemptions": exemptions}]})
        book = TranslatedBook("Test", "Author", [TranslatedChapter("I", [(source, korean)])])
        self.assertEqual(3, audit_and_correct(book, sheet, review)["contextual_exemptions"])
        self.assertEqual(korean, book.chapters[0].pairs[0][1])

    def test_evidence_may_cross_verse_labels_but_not_change_words(self):
        from quality_audit import _evidence_text
        source = "[4.686] οἳ θάμʼ ἀγειρόμενοι βίοτον κατακείρετε πολλόν,\n[4.687] κτῆσιν Τηλεμάχοιο δαΐφρονος·"
        quote = "βίοτον κατακείρετε πολλόν, κτῆσιν Τηλεμάχοιο"
        self.assertIn(_evidence_text(quote), _evidence_text(source))
        self.assertNotIn(_evidence_text(quote.replace("κατακείρετε", "κατακείρει")), _evidence_text(source))
        self.assertIn("[4.687]", _evidence_text("literal [4.687] in prose"))

    def test_valid_response_with_duplicate_closers_recovers_without_inventing_rows(self):
        from quality_audit import _json_object
        payload = {"paragraphs": [{"chapter_index": 1, "paragraph_index": 26, "action": "keep"}]}
        self.assertEqual(payload, _json_object(json.dumps(payload) + "]}"))
        self.assertIsNone(_json_object(json.dumps(payload) + '{"paragraphs":[]}'))
        self.assertIsNone(_json_object('{"paragraphs":['))

    def test_legacy_title_split_cannot_duplicate_the_korean_name(self):
        from finish_library_review import abbreviation_boundary_repetitions
        book = TranslatedBook("Test", "Author", [TranslatedChapter("I", [
            ("He asked Mr.", "그는 콜린스가"), ("Collins about his compliments.", "콜린스가 했던 찬사를 물었다.")])])
        self.assertEqual("콜린스가", abbreviation_boundary_repetitions(book)[0]["repeated_word"])
        book.chapters[0].pairs[0] = ("He asked Mr.", "그는 물었다.")
        self.assertEqual([], abbreviation_boundary_repetitions(book))
        book.chapters[0].pairs[0] = ("Collins spoke.", "콜린스가")
        self.assertEqual([], abbreviation_boundary_repetitions(book))

    def test_review_import_roundtrips_source_and_inline_formatting(self):
        book = {"title": "Test", "author": "Author", "chapters": [
            {"title": "I", "pairs": [("An <em>important</em> claim.", "<em>중요한</em> 주장이다.")]}]}
        with tempfile.TemporaryDirectory() as directory, patch("epub_validate.shutil.which", return_value=None), \
                patch("epub_validate.config.DATA_DIR", directory), patch.dict("os.environ", {"EPUBCHECK_JAR": "", "BABELFORGE_REQUIRE_EPUBCHECK": "0"}):
            path = assemble.assemble(book, str(Path(directory) / "book.epub"))
            imported = read_bilingual(Path(path))
        self.assertEqual(1, len(imported.chapters))
        self.assertIn("<em", imported.chapters[0].pairs[0][0])
        self.assertIn('data-bf-unit="0"', imported.chapters[0].template)
        self.assertNotIn("bf-translation", imported.chapters[0].template)


if __name__ == "__main__":
    unittest.main()
