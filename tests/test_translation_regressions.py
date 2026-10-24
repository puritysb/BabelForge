"""Offline regressions for source identity and final-text quality guarantees."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from extract import Chapter
from precision import choose_original_candidate, precision_profile_hash
from quality_audit import (QualityAuditError, audit_and_correct,
                           audit_book_quality, audit_translation_quality,
                           find_translation_integrity_violations, find_violations)
import translate
from translate import TranslatedBook, TranslatedChapter


def book(source, korean):
    return TranslatedBook("Test", "Author", [TranslatedChapter("I", [(source, korean)])])


def verdict(action="keep", text=None, **extra):
    row = {"chapter_index": 0, "paragraph_index": 0, "action": action,
           "reason": "source-grounded test verdict", **extra}
    if text is not None:
        row["corrected_ko"] = text
    return json.dumps({"paragraphs": [row]}, ensure_ascii=False)


TRUTH = {"concepts": [{"concept_id": "truth", "source_terms": ["Wahrheit"],
                       "canonical_ko": "진리", "avoid_ko": [], "strict": True}]}


class SourceIdentityRegressions(unittest.TestCase):
    def test_wrong_work_author_rights_and_volume_are_never_auto_selected(self):
        selected = {"source": "gutenberg", "source_id": "en",
                    "title": "Human, All-Too-Human", "author": "Nietzsche, Friedrich",
                    "language": "en", "rights": "public domain"}
        research = {"original_language": "de", "original_title": "Menschliches, Allzumenschliches",
                    "source_is_translation": True, "confidence": "high"}
        good = dict(selected, source_id="de", language="de",
                    title="Menschliches, Allzumenschliches")
        bad = [dict(good, title="Also sprach Zarathustra"),
               dict(good, author="Nietzsche, Elisabeth"),
               dict(good, rights="unknown"),
               dict(good, title="Menschliches, Allzumenschliches: Band II"),
               dict(good, source="annas_archive")]
        for candidate in bad:
            with self.subTest(candidate=candidate):
                chosen, replaced = choose_original_candidate(selected, research, lambda _: [candidate])
                self.assertFalse(replaced)
                self.assertEqual(selected, chosen)

    def test_explicit_volume_cannot_be_replaced_with_unidentified_whole_work(self):
        selected = {"source": "gutenberg", "title": "Human, All-Too-Human, Volume II",
                    "author": "Nietzsche, Friedrich", "language": "en", "rights": "public domain"}
        research = {"original_language": "de", "original_title": "Menschliches, Allzumenschliches",
                    "source_is_translation": True, "confidence": "high"}
        candidate = dict(selected, title="Menschliches, Allzumenschliches", language="de")
        self.assertFalse(choose_original_candidate(selected, research, lambda _: [candidate])[1])


class FinalQualityRegressions(unittest.TestCase):
    def test_terminology_duplicate_verdicts_never_choose_first_response(self):
        row={'chapter_index':0,'paragraph_index':0,'action':'correct','corrected_ko':'진리는 남는다.'}
        with self.assertRaises(QualityAuditError):
            audit_and_correct(book('Die Wahrheit bleibt.','사실은 남는다.'),TRUTH,
                              lambda _:json.dumps({'paragraphs':[row,row]}))

    def test_terminology_indices_are_not_coerced_from_other_json_types(self):
        for index in [False,0.4,'0']:
            with self.subTest(index=index),self.assertRaises(QualityAuditError):
                row={'chapter_index':index,'paragraph_index':0,'action':'correct','corrected_ko':'진리는 남는다.'}
                audit_and_correct(book('Die Wahrheit bleibt.','사실은 남는다.'),TRUTH,
                                  lambda _:json.dumps({'paragraphs':[row]}))

    def test_cjk_and_short_untranslated_text_cannot_be_kept(self):
        for source in ("吾輩は猫である。名前はまだ無い。", "吾生也有涯，而知也無涯。", "No!", "Non."):
            with self.subTest(source=source):
                subject = book(source, source)
                with self.assertRaises(QualityAuditError):
                    audit_translation_quality(subject, lambda _: verdict())

    def test_numeric_or_roman_numeral_labels_need_no_korean(self):
        for source in ("123", "IV.", "—"):
            audit_translation_quality(book(source, source), lambda _: verdict())

    def test_idiom_exemption_cannot_cover_separate_standalone_occurrence(self):
        subject = book("Die Wahrheit bleibt, aber in Wahrheit zweifle ich.",
                       "사실은 남지만 실은 나는 의심한다.")
        reply = verdict(text=subject.chapters[0].pairs[0][1], source_sense_exemptions=[{
            "concept_id": "truth", "source_expression": "in Wahrheit", "reason": "idiom"}])
        with self.assertRaises(QualityAuditError):
            audit_and_correct(subject, TRUTH, lambda _: reply)

    def test_repeated_idiom_requires_occurrence_identification(self):
        subject = book("In Wahrheit kommt er. In Wahrheit bleibt er.", "실은 그는 온다. 실은 그는 남는다.")
        reply = verdict(text=subject.chapters[0].pairs[0][1], source_sense_exemptions=[{
            "concept_id": "truth", "source_expression": "In Wahrheit", "reason": "idiom"}])
        with self.assertRaises(QualityAuditError):
            audit_and_correct(subject, TRUTH, lambda _: reply)

    def test_terms_are_matched_in_visible_text_not_html_attributes(self):
        subject = book('Die <em class="concept">Wahrheit</em> bleibt.', '진<strong>리</strong>는 남는다.')
        self.assertEqual([], find_violations(subject, TRUTH))
        subject.chapters[0].pairs[0] = (subject.chapters[0].pairs[0][0], '<span title="진리">사실</span>은 남는다.')
        self.assertEqual(1, len(find_violations(subject, TRUTH)))

    def test_cjk_concepts_match_inside_unspaced_prose(self):
        subject = book("仁者爱人。", "어진 이는 사람을 사랑한다.")
        sheet = {"concepts": [{"concept_id": "ren", "source_terms": ["仁"],
                               "canonical_ko": "인", "strict": True}]}
        self.assertEqual(1, len(find_violations(subject, sheet)))

    def test_correction_can_add_canonical_and_preserve_different_lemma(self):
        subject = book("Erkenntniss und Wissen bleiben.", "지식과 지식은 남는다.")
        sheet = {"concepts": [{"concept_id": "knowing", "source_terms": ["Erkenntniss"],
                               "canonical_ko": "인식", "avoid_ko": ["지식"], "strict": True}]}
        response = verdict("correct", "인식과 지식은 남는다.", contextual_exemptions=[{
            "concept_id": "knowing", "avoid_ko": "지식", "source_expression": "Wissen",
            "reason": "different source lemma"}])
        self.assertTrue(audit_and_correct(subject, sheet, lambda _: response)["passed"])

    def test_terminology_repair_is_semantically_reviewed_before_success(self):
        subject = book("Die Wahrheit bleibt.", "사실은 남는다.")
        calls = []
        def chat(messages):
            calls.append(messages[0]["content"])
            return verdict("correct", "진리는 남는다.") if "terminology auditor" in calls[-1] else verdict()
        report = audit_book_quality(subject, TRUTH, chat)
        self.assertTrue(report["passed"])
        self.assertEqual(2, sum("whole-book Korean" in prompt for prompt in calls))
        self.assertEqual(2, report["audit_rounds"])

    def test_terminology_repair_cannot_publish_untranslated_final_text(self):
        source = "In truth, the entire journey began with a simple misunderstanding."
        subject = book(source, "실은 그 여행 전체는 단순한 오해에서 시작되었다.")
        sheet = {"concepts": [dict(TRUTH["concepts"][0], source_terms=["truth"])]}
        def chat(messages):
            if "terminology auditor" in messages[0]["content"]:
                return verdict("correct", source, source_sense_exemptions=[{
                    "concept_id": "truth", "source_expression": "In truth", "reason": "idiom"}])
            return verdict()
        with self.assertRaises(QualityAuditError):
            audit_book_quality(subject, sheet, chat)

    def test_conflicting_semantic_and_terminology_repairs_fail_boundedly(self):
        subject = book("Die Wahrheit bleibt.", "사실은 남는다.")
        calls = []
        def chat(messages):
            calls.append(messages)
            return verdict("correct", "진리는 남는다." if "terminology auditor" in messages[0]["content"] else "사실은 남는다.", source_quote="Die Wahrheit bleibt.")
        with self.assertRaisesRegex(QualityAuditError, "did not converge"):
            audit_book_quality(subject, TRUTH, chat, max_rounds=2)
        self.assertLessEqual(len(calls), 5)

    def test_cached_approval_requires_exact_current_text(self):
        subject = book("The truth remains.", "진리는 남는다.")
        verified = set()
        audit_translation_quality(subject, lambda _: verdict(), verified=verified)
        report = audit_translation_quality(subject, lambda _: self.fail("cache miss"), verified=verified)
        self.assertEqual(1, report["semantic_paragraphs_cached"])
        subject.chapters[0].pairs[0] = ("The truth remains.", "The truth remains.")
        with self.assertRaises(QualityAuditError):
            audit_translation_quality(subject, lambda _: verdict(), verified=verified)

    def test_focused_review_uses_source_context_and_invalidates_changed_context(self):
        subject = book("Odysseus spoke to Telemachus.", "오디세우스가 텔레마코스에게 말했다.")
        subject.chapters[0].pairs.append(("He took off his tunic.", "그는 튜닉을 벗었다."))
        verified = set()
        calls = []

        def chat(messages):
            row = json.loads(messages[1]["content"])["paragraphs"][0]
            calls.append(row)
            self.assertEqual({"title": "Test", "author": "Author"}, row["work"])
            self.assertEqual(subject.chapters[0].pairs[0][0], row["previous_source"])
            self.assertEqual("", row["next_source"])
            return verdict(paragraph_index=1)

        audit_translation_quality(subject, chat, paragraph_keys={(0, 1)}, verified=verified)
        subject.chapters[0].pairs[0] = ("Telemachus went to bed.", "텔레마코스가 잠자리에 들었다.")
        audit_translation_quality(subject, chat, paragraph_keys={(0, 1)}, verified=verified)
        self.assertEqual(2, len(calls))


class CheckpointRegressions(unittest.TestCase):
    def _sheet(self, chapters, target="진리"):
        sheet = {"schema_version": 1, "profile_version": "test-v1",
                 "source_hash": translate._source_fingerprint(chapters),
                 "concepts": [{"source_terms": ["truth"], "canonical_ko": target,
                               "concept_id": "truth", "strict": False}]}
        sheet["profile_hash"] = precision_profile_hash(sheet)
        return sheet

    def _write(self, path, chapters, sheet):
        translate._save_checkpoint(path, "Test", "Author", translate._source_fingerprint(chapters), "en",
            [TranslatedChapter(ch.title, [(p, "기존 번역문이다.") for p in ch.paragraphs]) for ch in chapters],
            quality_profile_hash=translate._quality_profile_fingerprint(sheet),
            translation_profile_hash=translate._instruction_profile_fingerprint(sheet, {}, "en"))

    def test_profile_change_cannot_reuse_structural_remap(self):
        chapters = [Chapter("I", ["The truth remains."])]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "book.json")
            self._write(path, chapters, self._sheet(chapters))
            with patch("translate._translate_batch", return_value=["새 번역문이다."]) as draft, \
                 patch("translate._chat_json", return_value=verdict()):
                result = translate.translate_book("Test", "Author", chapters, checkpoint_path=path,
                    glossary={}, concept_sheet=self._sheet(chapters, "참됨"), max_workers=1)
            draft.assert_called_once()
            self.assertEqual("새 번역문이다.", result.chapters[0].pairs[0][1])

    def test_source_hash_header_does_not_authorize_misaligned_cached_pairs(self):
        chapters=[Chapter("I",["The truth remains."])]
        sheet=self._sheet(chapters)
        with tempfile.TemporaryDirectory() as directory:
            path=os.path.join(directory,"book.json");self._write(path,chapters,sheet)
            with open(path,encoding="utf-8") as stream:saved=json.load(stream)
            saved['chapters'][0]['pairs'][0][0]='A different source.'
            with open(path,'w',encoding='utf-8') as stream:json.dump(saved,stream)
            with patch('translate._translate_batch',return_value=['새 번역문이다.']) as draft, \
                 patch('translate._chat_json',return_value=verdict()):
                result=translate.translate_book('Test','Author',chapters,checkpoint_path=path,
                    glossary={},concept_sheet=sheet,max_workers=1)
            draft.assert_called_once()
            self.assertEqual('새 번역문이다.',result.chapters[0].pairs[0][1])

    def test_structure_only_remap_uses_source_independent_instruction_hash(self):
        old = [Chapter("Untitled", ["The truth remains."])]
        new = [Chapter("Real chapter", ["The truth remains."])]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "book.json")
            self._write(path, old, self._sheet(old))
            with patch("translate._translate_batch") as draft, \
                 patch("translate._chat_json", return_value=verdict()) as review:
                result = translate.translate_book("Test", "Author", new, checkpoint_path=path,
                    glossary={}, concept_sheet=self._sheet(new), max_workers=1)
            draft.assert_not_called()
            review.assert_called_once()
            self.assertEqual("기존 번역문이다.", result.chapters[0].pairs[0][1])

    def test_semantic_corrections_and_approvals_resume_after_later_failure(self):
        chapters = [Chapter("I", ["The first passage remains.", "The second passage remains."])]
        sheet = {"profile_hash": "test", "concepts": []}
        calls = []
        def first_run(messages):
            calls.append(messages)
            if len(calls) == 1:
                return verdict("correct", "첫 구절은 남는다.", source_quote="The first passage remains.")
            raise translate.TranslationError("simulated network failure")
        resumed_keys = []
        def resume(messages):
            rows = json.loads(messages[1]["content"])["paragraphs"]
            key = rows[0]["paragraph_index"]
            resumed_keys.append(key)
            # The repaired first passage was saved but NEVER approved before
            # interruption. Both passages require an explicit keep on resume.
            return verdict(paragraph_index=key)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "book.json")
            with patch("translate.config.QUALITY_AUDIT_BATCH_PARAGRAPHS", 1), \
                 patch("translate._translate_batch", return_value=["첫 번역이다.", "둘째 구절은 남는다."]), \
                 patch("translate._chat_json", side_effect=first_run):
                with self.assertRaisesRegex(translate.TranslationError, "simulated"):
                    translate.translate_book("Test", "Author", chapters, checkpoint_path=path,
                        glossary={}, concept_sheet=sheet, max_workers=1)
            # Another interruption before any accepted response must not erase
            # the previous run's saved correction ledger during pre-audit flush.
            with patch("translate._translate_batch"), \
                 patch("translate._chat_json", side_effect=translate.TranslationError("again")):
                with self.assertRaisesRegex(translate.TranslationError, "again"):
                    translate.translate_book("Test", "Author", chapters, checkpoint_path=path,
                        glossary={}, concept_sheet=sheet, max_workers=1)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(1, len(json.load(handle)["audit_log"]))
            with patch("translate.config.QUALITY_AUDIT_BATCH_PARAGRAPHS", 1), \
                 patch("translate._translate_batch") as draft, \
                 patch("translate._chat_json", side_effect=resume) as review:
                result = translate.translate_book("Test", "Author", chapters, checkpoint_path=path,
                    glossary={}, concept_sheet=sheet, max_workers=1)
            draft.assert_not_called()
            self.assertEqual([0, 1], resumed_keys)
            self.assertEqual(2, review.call_count)
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual([False, True, True], [r["approved"] for r in saved["audit_log"]])
            self.assertEqual("첫 구절은 남는다.", result.chapters[0].pairs[0][1])
            self.assertTrue(result.quality_report["passed"])

    def test_live_template_and_assets_survive_translation_not_checkpoint(self):
        chapter = Chapter("I", ["A short sentence."])
        chapter.template = '<section data-bf="0"/>'
        chapter.assets = {"image.png": b"binary image"}
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "book.json")
            with patch("translate._translate_batch", return_value=["짧은 문장이다."]), \
                 patch("translate._chat_json", return_value=verdict()):
                result = translate.translate_book("Test", "Author", [chapter], checkpoint_path=path,
                    glossary={}, concept_sheet={"concepts": []}, max_workers=1)
            self.assertEqual(chapter.template, result.chapters[0].template)
            self.assertEqual(chapter.assets, result.chapters[0].assets)
            with open(path, encoding="utf-8") as handle:
                checkpoint = json.load(handle)
            self.assertNotIn("assets", checkpoint["chapters"][0])


class InlineTranslationRegressions(unittest.TestCase):
    def test_failed_tagged_draft_is_not_replaced_by_unprotected_proofread(self):
        with patch("translate._chat") as chat:
            result = translate._proofread_batch(['<em class="voice">A voice.</em>'], [""], "en")
        self.assertEqual([""], result)
        chat.assert_not_called()

    def test_tag_attributes_are_preserved_and_missing_marker_falls_back(self):
        source = '<em class="voice" lang="en">A voice.</em>'
        stashed, mapping = translate._stash_tags(source)
        with patch("translate._chat", return_value="[1] 목소리다."), \
             patch("translate._translate_one", return_value='<em class="voice" lang="en">목소리다.</em>') as fallback, \
             patch("translate._proofread_batch", side_effect=lambda src, tr, *args: tr):
            translated = translate._translate_batch([source])
        fallback.assert_called_once()
        self.assertIn('class="voice"', translated[0])
        self.assertEqual(source, translate._restore_tags(stashed, mapping))


if __name__ == "__main__":
    unittest.main()
