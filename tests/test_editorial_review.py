"""Protocol regressions from the Odyssey correction, with no live API calls.

These verify editorial safety properties, not an LLM's language competence.
"""
import json
import hashlib
import tempfile
import unittest
import unicodedata
from pathlib import Path
from unittest.mock import patch

from lxml import etree

import assemble
from epub_validate import EpubValidationError, validate_epub
from quality_audit import QualityAuditError, audit_and_correct, audit_translation_quality
from translate import TranslatedBook, TranslatedChapter


def subject(source="He took off his tunic.", korean="하녀가 그의 튜닉을 벗겼다."):
    return TranslatedBook("A narrative", "Author", [TranslatedChapter("I", [(source, korean)])])


def reply(rows):
    return json.dumps({"paragraphs": rows}, ensure_ascii=False)


def decision(index=0, action="keep", text=None, **kw):
    return {"chapter_index": 0, "paragraph_index": index, "action": action,
            "corrected_ko": text, "reason": "source-grounded correction", **kw}


class IndependentEditorialReview(unittest.TestCase):
    def test_cited_evidence_is_exact_source_scoped_and_invalidates_only_its_approval(self):
        from quality_audit import _semantic_rows, _semantic_signature
        book = subject()
        book.chapters[0].pairs.append(("Another passage.", "다른 구절이다."))
        before = _semantic_rows(book, None, {})
        key = hashlib.sha256(book.chapters[0].pairs[0][0].encode()).hexdigest()
        book.source_evidence = {key: {"fact": "A cited lexical sense", "url": "https://example.org/lexicon"}}
        after = _semantic_rows(book, None, {})
        self.assertNotEqual(_semantic_signature(before[0], before[0]['current_ko']),
                            _semantic_signature(after[0], after[0]['current_ko']))
        self.assertEqual(_semantic_signature(before[1], before[1]['current_ko']),
                         _semantic_signature(after[1], after[1]['current_ko']))
        approvals = set()
        def chat(messages):
            rows = json.loads(messages[1]['content'])['paragraphs']
            self.assertEqual(book.source_evidence[key], rows[0]['audit_profile']['source_evidence'])
            return reply([decision(index=i) for i in range(2)])
        audit_translation_quality(book, chat, verified=approvals)
        book.source_evidence[key]['fact'] = 'Revised evidence'
        changed = _semantic_rows(book, None, {})
        self.assertNotIn(_semantic_signature(changed[0], changed[0]['current_ko']), approvals)
        book.chapters[0].pairs[0] = ('Changed source.', book.chapters[0].pairs[0][1])
        self.assertNotIn('source_evidence', _semantic_rows(book, None, {})[0]['audit_profile'])

    def test_source_evidence_survives_review_checkpoint_roundtrip(self):
        from review_library import serialize, deserialize
        book = subject()
        book.source_evidence = {'exact-source-hash': {'fact': 'dictionary evidence'}}
        restored = deserialize(json.loads(json.dumps(serialize(book))))
        self.assertEqual(book.source_evidence, restored.source_evidence)

    def test_repaired_wording_needs_fresh_exact_final_approval(self):
        book = subject()
        approvals, saved = set(), []
        calls = []
        def chat(messages):
            row = json.loads(messages[1]["content"])["paragraphs"][0]
            calls.append(row)
            if len(calls) == 1:
                return reply([decision(action="correct", text="그가 튜닉을 벗었다.",
                                       source_quote="He took off his tunic.")])
            self.assertEqual(set(), approvals)
            self.assertNotIn("reason", row)  # no proposer rationale anchoring
            self.assertEqual("그가 튜닉을 벗었다.", row["current_ko"])
            return reply([decision()])
        report = audit_translation_quality(book, chat, verified=approvals,
            progress_cb=lambda: saved.append((len(approvals), book.chapters[0].pairs[0][1])))
        self.assertEqual([(0, "그가 튜닉을 벗었다."), (1, "그가 튜닉을 벗었다.")], saved)
        self.assertEqual(1, report["semantic_paragraphs_corrected"])
        self.assertEqual([False, True], [r["approved"] for r in book.audit_log])
        self.assertEqual(book.audit_log[0]["output_signature"], book.audit_log[1]["input_signature"])

    def test_corrector_cannot_approve_its_own_regression(self):
        book = subject("The living trunk remained rooted as a bedpost.", "뿌리째 선 줄기가 침대 기둥이 되었다.")
        responses = iter([
            reply([decision(action="correct", text="뿌리에서 잘라 낸 줄기가 침대 기둥이 되었다.", source_quote="The living trunk remained rooted as a bedpost.")]),
            reply([decision(action="correct", text="뿌리째 선 줄기가 침대 기둥이 되었다.", source_quote="The living trunk remained rooted as a bedpost.")]),
            reply([decision()])])
        approvals = set()
        audit_translation_quality(book, lambda _: next(responses), verified=approvals)
        self.assertEqual(1, len(approvals))
        self.assertEqual([False, False, True], [r["approved"] for r in book.audit_log])

    def test_repeated_corrections_never_become_approval(self):
        book = subject()
        approvals = set()
        with self.assertRaisesRegex(QualityAuditError, "did not converge"):
            audit_translation_quality(book, lambda _: reply([decision(action="correct", text="그는 튜닉을 벗었다.", source_quote="He took off his tunic.")]),
                                      verified=approvals, max_review_rounds=3)
        self.assertFalse(approvals)
        self.assertEqual(3, len(book.audit_log))

    def test_partial_response_saves_good_rows_and_retries_only_invalid_rows(self):
        book = subject()
        book.chapters[0].pairs.append(("She left.", "그녀는 떠났다."))
        calls = []
        approvals = set()
        def chat(messages):
            rows = json.loads(messages[1]["content"])["paragraphs"]
            calls.append([r["paragraph_index"] for r in rows])
            if len(calls) == 1:
                return reply([decision(0), decision(1, action="invalid")])
            self.assertEqual(1, len(approvals))
            return "```json\n" + reply([decision(1)]) + "\n```"
        audit_translation_quality(book, chat, verified=approvals)
        self.assertEqual([[0, 1], [1]], calls)
        self.assertEqual(2, len(approvals))

    def test_duplicate_verdict_does_not_create_approval(self):
        book = subject()
        approvals = set()
        with self.assertRaises(QualityAuditError):
            audit_translation_quality(book, lambda _: reply([decision(), decision()]), verified=approvals)
        self.assertEqual(set(), approvals)
        self.assertEqual([], book.audit_log)

    def test_evidence_allows_unicode_equivalence_but_not_wrong_inflection(self):
        for quote, valid in [(unicodedata.normalize("NFD", "ἔξοχοι"), True), ("ἔξοχαι", False)]:
            with self.subTest(quote=quote):
                book = subject("ἔξοχοι", "뛰어난 이들이다.")
                calls = []
                def chat(messages):
                    calls.append(messages)
                    return reply([decision(action="correct", text="빼어난 이들이다.", source_quote=quote)]) if len(calls) == 1 or not valid else reply([decision()])
                if valid:
                    audit_translation_quality(book, chat)
                    self.assertEqual(2, len(calls))
                else:
                    with self.assertRaises(QualityAuditError):
                        audit_translation_quality(book, chat)
                    self.assertEqual("뛰어난 이들이다.", book.chapters[0].pairs[0][1])

    def test_model_or_concept_profile_change_invalidates_approval(self):
        book = subject(korean="그는 튜닉을 벗었다.")
        approvals, calls = set(), []
        def chat(messages):
            calls.append(messages)
            return reply([decision()])
        for profile in ({"model": "A", "concept": "v1"}, {"model": "A", "concept": "v1"},
                        {"model": "B", "concept": "v1"}, {"model": "B", "concept": "v2"}):
            audit_translation_quality(book, chat, verified=approvals, audit_profile=profile)
        self.assertEqual(3, len(calls))

    def test_character_budget_splits_batches_and_never_truncates_unit(self):
        book = subject("a" * 500, "번역문이다.")
        book.chapters[0].pairs.append(("b" * 500, "다른 번역문이다."))
        sizes = []
        def chat(messages):
            rows = json.loads(messages[1]["content"])["paragraphs"]
            sizes.append(len(rows))
            return reply([decision(r["paragraph_index"]) for r in rows])
        audit_translation_quality(book, chat, max_batch_chars=2000)
        self.assertEqual([1, 1], sizes)
        with self.assertRaisesRegex(QualityAuditError, "character budget"):
            audit_translation_quality(book, lambda _: self.fail("oversized API call"), max_batch_chars=100)

    def test_repair_invalidates_neighbor_approval_for_register_and_boundary_checks(self):
        book = subject()
        book.chapters[0].pairs.append(("She waited.", "그녀는 기다렸다."))
        calls = []
        def chat(messages):
            rows = json.loads(messages[1]["content"])["paragraphs"]
            calls.append(rows)
            if len(calls) == 1:
                return reply([decision(0, action="correct", text="그는 튜닉을 벗었다.",
                                       source_quote="He took off his tunic."), decision(1)])
            self.assertEqual([0, 1], [r["paragraph_index"] for r in rows])
            self.assertEqual("그는 튜닉을 벗었다.", rows[1]["previous_ko"])
            return reply([decision(0), decision(1)])
        audit_translation_quality(book, chat)
        self.assertEqual(2, len(calls))
        self.assertEqual(2, sum(r["paragraph_index"] == 1 for r in book.audit_log))

    def test_context_crosses_empty_chapters_and_korean_changes_invalidate_cache(self):
        book = subject(korean="그는 튜닉을 벗었다.")
        book.chapters.extend([TranslatedChapter("Empty", []),
                              TranslatedChapter("II", [("He lay down.", "그는 누웠다.")])])
        approvals, calls = set(), []
        def chat(messages):
            row = json.loads(messages[1]["content"])["paragraphs"][0]
            calls.append(row)
            self.assertEqual("He took off his tunic.", row["previous_source"])
            return reply([{**decision(), "chapter_index": 2}])
        audit_translation_quality(book, chat, paragraph_keys={(2, 0)}, verified=approvals)
        book.chapters[0].pairs[0] = (book.chapters[0].pairs[0][0], "그는 튜닉을 벗었소.")
        audit_translation_quality(book, chat, paragraph_keys={(2, 0)}, verified=approvals)
        self.assertEqual(2, len(calls))
        self.assertEqual("그는 튜닉을 벗었소.", calls[1]["previous_ko"])

    def test_missing_semantic_evidence_cannot_be_applied(self):
        book = subject()
        with self.assertRaises(QualityAuditError):
            audit_translation_quality(book, lambda _: reply([
                decision(action="correct", text="그는 튜닉을 벗었다.")]))
        self.assertEqual("하녀가 그의 튜닉을 벗겼다.", book.chapters[0].pairs[0][1])
        self.assertEqual([], book.audit_log)

    def test_korean_only_typo_repair_still_needs_independent_approval(self):
        book = subject("Dawn came.", "새벍이 왔다.")
        responses = iter([reply([decision(action="correct", text="새벽이 왔다.", category="korean")]),
                          reply([decision()])])
        audit_translation_quality(book, lambda _: next(responses))
        self.assertEqual([False, True], [r["approved"] for r in book.audit_log])

    def test_repair_budget_bounds_full_rewrite_and_evidence_not_just_input(self):
        book = subject("a" * 700, "번역문이다.")
        book.chapters[0].pairs.append(("b" * 700, "다른 번역문이다."))
        sizes = []
        def chat(messages):
            rows = json.loads(messages[1]["content"])["paragraphs"]
            sizes.append(len(rows))
            return reply([decision(r["paragraph_index"]) for r in rows])
        audit_translation_quality(book, chat, max_batch_chars=24000, max_repair_chars=1500)
        self.assertEqual([1, 1], sizes)

    def test_unchanged_valid_prose_in_other_languages_is_not_rewritten(self):
        for source, korean in [("Er hätte lieber den Tod gewählt.", "그는 차라리 죽음을 택했을 것이다."),
                               ("吾輩は猫である。", "나는 고양이다."),
                               ("If you are a goddess, hear me.", "그대가 여신이라면 내 말을 들어 주시오.")]:
            with self.subTest(source=source):
                book = subject(source, korean)
                report = audit_translation_quality(book, lambda _: reply([decision()]))
                self.assertEqual(korean, book.chapters[0].pairs[0][1])
                self.assertEqual(0, report["semantic_paragraphs_corrected"])


class OccurrenceScopedTerminology(unittest.TestCase):
    sheet = {"concepts": [{"concept_id": "eos", "source_terms": ["Eos"],
        "canonical_ko": "에오스", "avoid_ko": ["이오스"], "strict": True}]}
    source = "Eos saw the Achaeans and Eos spoke."
    korean = "에오스가 아카이오스인을 보고 이오스가 말했다."

    def test_one_substring_collision_cannot_exempt_second_mistranslation(self):
        for identify in (False, True):
            with self.subTest(identify=identify):
                exemption = {"concept_id": "eos", "avoid_ko": "이오스",
                             "source_expression": "Achaeans", "reason": "different people"}
                if identify:
                    exemption["ko_start"] = self.korean.index("이오스")
                with self.assertRaises(QualityAuditError):
                    audit_and_correct(subject(self.source, self.korean), self.sheet,
                        lambda _: reply([decision(text=self.korean, contextual_exemptions=[exemption])]))

    def test_correcting_other_occurrence_preserves_legitimate_substring_after_move(self):
        final = "먼저 에오스가 아카이오스인을 보았고 에오스가 말했다."
        book = subject(self.source, self.korean)
        report = audit_and_correct(book, self.sheet, lambda _: reply([
            decision(action="correct", text=final, contextual_exemptions=[{
                "concept_id": "eos", "avoid_ko": "이오스", "source_expression": "Achaeans",
                "reason": "ethnonym, not the goddess"}])]))
        self.assertTrue(report["passed"])
        self.assertEqual(final, book.chapters[0].pairs[0][1])

    def test_multiple_legitimate_occurrences_need_separate_offsets(self):
        source = "Eos met Achaeans and Achaean women."
        korean = "에오스가 아카이오스인과 아카이오스 여인들을 만났다."
        starts = [i for i in range(len(korean)) if korean.startswith("이오스", i)]
        exemptions = [{"concept_id": "eos", "avoid_ko": "이오스", "source_expression": expression,
                       "reason": "distinct people", "ko_start": start}
                      for start, expression in zip(starts, ["Achaeans", "Achaean women"])]
        book = subject(source, korean)
        report = audit_and_correct(book, self.sheet, lambda _: reply([
            decision(text=korean, contextual_exemptions=exemptions)]))
        self.assertEqual(2, report["contextual_exemptions"])
        self.assertEqual(korean, book.chapters[0].pairs[0][1])


class TranslationFailureRecovery(unittest.TestCase):
    def test_unexpected_worker_failure_preserves_other_completed_results(self):
        import translate
        from extract import Chapter
        chapters = [Chapter("I", ["The first passage."]), Chapter("II", ["The second passage."])]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            with patch("translate._translate_batch", side_effect=[RuntimeError("parser failure"), ["둘째 구절."]]), \
                    patch("translate._chat_json") as audit:
                with self.assertRaises(translate.TranslationError):
                    translate.translate_book("Test", "Author", chapters, checkpoint_path=str(path),
                        glossary={}, concept_sheet={"concepts": []}, max_workers=1)
            saved = json.loads(path.read_text())
            self.assertEqual("", saved["chapters"][0]["pairs"][0][1])
            self.assertEqual("둘째 구절.", saved["chapters"][1]["pairs"][0][1])
            audit.assert_not_called()


class FinalArtifactConservation(unittest.TestCase):
    def test_equal_pair_counts_cannot_hide_changed_text_or_reading_order(self):
        pairs = [("First.", "처음."), ("Second.", "다음.")]
        book = {"title": "Test", "chapters": [{"title": "I", "pairs": pairs}]}
        with tempfile.TemporaryDirectory() as directory, patch('epub_validate.shutil.which', return_value=None), \
                patch('epub_validate.config.DATA_DIR', directory), patch.dict('os.environ', {'EPUBCHECK_JAR': '', 'BABELFORGE_REQUIRE_EPUBCHECK': '0'}):
            path = assemble.assemble(book, str(Path(directory) / "test.epub"))
            self.assertTrue(validate_epub(path, external=False, expected_content=pairs)["content_compared"])
            for changed in ([pairs[1], pairs[0]], [("Changed.", "처음."), pairs[1]],
                            [("First.", "오역."), pairs[1]]):
                with self.assertRaisesRegex(EpubValidationError, "reading order"):
                    validate_epub(path, expected_pairs=2, external=False, expected_content=changed)

    def test_navigation_marks_original_and_translation_languages(self):
        root = etree.fromstring(assemble._build_nav_xhtml("Test", [(1, "Λόγος")], ["이야기"], "grc", "ko").encode())
        ns = {"x": "http://www.w3.org/1999/xhtml"}
        self.assertEqual(["grc", "ko"], root.xpath("//x:a/x:span/@lang", namespaces=ns))
        self.assertEqual("grc", root.get("lang"))


if __name__ == "__main__":
    unittest.main()
