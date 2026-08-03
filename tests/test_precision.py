from __future__ import annotations

import json
import os
import tempfile
import unittest

from extract import Chapter
from precision import (_call_json, _validate_concepts, build_concept_sheet, choose_original_candidate,
                       _related_lexical_candidates, extract_repeated_phrases, flat_glossary,
                       precision_profile_hash)
from quality_audit import audit_and_correct, find_violations
from translate import (TranslatedBook, TranslatedChapter, _concept_rules,
                       _quality_profile_fingerprint, translate_book)
from unittest.mock import patch


class PrecisionResearchTests(unittest.TestCase):
    def test_malformed_json_is_repaired_autonomously(self):
        replies = iter(['{"concepts":[{"broken":',
                        '{"concepts":[{"fixed":true}]}'])
        calls = []

        def chat(messages):
            calls.append(messages)
            return next(replies)

        parsed = _call_json(chat, "system", "user", "test stage")
        self.assertTrue(parsed["concepts"][0]["fixed"])
        self.assertEqual(2, len(calls))
        self.assertIn("repair", calls[1][0]["content"])

    def test_repeated_phrase_extraction_includes_lowercase_concept(self):
        text = (
            "A free spirit questions inherited values. The free spirit wanders. "
            "Every free spirit asks for reasons. A free spirit remains distinct."
        )
        terms = {row["term"].lower() for row in extract_repeated_phrases(text)}
        self.assertIn("free spirit", terms)

    def test_rare_lexical_neighbor_is_not_lost_to_frequency_filter(self):
        text = (
            "Freigeist Freigeist Freigeister Freigeistes. "
            "Ein jüdischer Freidenker erscheint nur einmal."
        )
        related = _related_lexical_candidates(text, [{
            "source_terms": ["Freigeist", "Freigeister", "Freigeistes"]
        }])
        terms = {row["source_terms"][0] for row in related}
        self.assertIn("Freidenker", terms)

    def test_unsafe_fragments_and_alternative_targets_are_rejected(self):
        text = "Der freie Geist sieht höhere Kultur und niedere Kultur."
        raw = [{
            "concept_id": "freigeist", "source_terms": ["freie Geist", "freie"],
            "canonical_ko": "자유정신", "strict": True,
        }, {
            "concept_id": "culture_pair",
            "source_terms": ["höhere Kultur", "niedere Kultur"],
            "canonical_ko": "고등 문화 / 저등 문화", "strict": True,
        }]
        concepts = _validate_concepts(raw, text, set())
        self.assertEqual(1, len(concepts))
        self.assertEqual(["freie Geist"], concepts[0]["source_terms"])

    def test_original_language_candidate_is_selected_autonomously(self):
        selected = {
            "source": "gutenberg", "source_id": "51935",
            "title": "Human, All-Too-Human", "author": "Nietzsche, Friedrich",
            "language": "en", "rights": "public domain", "extra": {},
        }
        research = {
            "original_language": "de", "original_title": "Menschliches, Allzumenschliches",
            "source_is_translation": True, "confidence": "high",
        }
        german = {
            "source": "gutenberg", "source_id": "7207",
            "title": "Menschliches, Allzumenschliches: Ein Buch für freie Geister",
            "author": "Nietzsche, Friedrich Wilhelm", "language": "de",
            "rights": "public domain", "extra": {},
        }
        chosen, replaced = choose_original_candidate(
            selected, research, lambda query: [german])
        self.assertTrue(replaced)
        self.assertEqual("7207", chosen["source_id"])
        self.assertIn("precision_source_replaced", chosen["extra"])

    def test_low_confidence_source_research_never_replaces_edition(self):
        selected = {"source": "gutenberg", "source_id": "1", "title": "X",
                    "author": "A", "language": "en", "extra": {}}
        research = {"original_language": "de", "original_title": "Y",
                    "source_is_translation": True, "confidence": "low"}
        chosen, replaced = choose_original_candidate(
            selected, research, lambda query: [dict(selected, language="de")])
        self.assertFalse(replaced)
        self.assertEqual(selected, chosen)

    def test_concept_sheet_uses_web_adjudication_and_adversarial_review(self):
        chapters = [Chapter(title="V", paragraphs=[
            "The free spirit is unlike the fettered spirit.",
            "A free spirit asks for reasons.",
            "The free spirit is not merely a free-thinker.",
        ])]
        calls = []

        final_concepts = [{
            "concept_id": "nietzsche.freigeist",
            "source_terms": ["free spirit"],
            "original_terms": ["Freigeist"],
            "canonical_ko": "자유정신",
            "avoid_ko": ["자유사상가", "자유로운 영혼"],
            "kind": "concept",
            "rationale": "관습에서 해방된 정신의 유형",
            "confidence": "high",
            "strict": True,
            "evidence_urls": ["https://example.org/primary"],
        }]

        def chat(messages):
            calls.append(messages)
            if len(calls) == 1:
                return json.dumps({"concepts": [{
                    "source_terms": ["free spirit"],
                    "original_terms": ["Freigeist"],
                    "kind": "concept", "why_important": "central",
                    "search_query": "Freigeist Nietzsche Korean",
                }]})
            key = "concepts" if len(calls) in {2, 4} else "proposed_concepts"
            return json.dumps({key: final_concepts}, ensure_ascii=False)

        def search(query, count=4):
            return [{"title": "Primary text", "link": "https://example.org/primary",
                     "content": "Freigeist in the critical text"}]

        sheet = build_concept_sheet(
            chapters, "Human, All Too Human", "Nietzsche", "en",
            {"source_is_translation": True, "original_language": "de"},
            chat, search)
        self.assertEqual(4, len(calls))
        self.assertEqual("자유정신", flat_glossary(sheet)["free spirit"])
        self.assertEqual("high", sheet["concepts"][0]["confidence"])
        self.assertEqual(sheet["profile_hash"], precision_profile_hash(sheet))

    def test_profile_changes_when_concept_decision_changes(self):
        a = {"profile_hash": "a", "concepts": [{"canonical_ko": "자유정신"}]}
        b = {"profile_hash": "b", "concepts": [{"canonical_ko": "자유사상가"}]}
        self.assertNotEqual(_quality_profile_fingerprint(a),
                            _quality_profile_fingerprint(b))

    def test_research_snippet_churn_does_not_invalidate_profile(self):
        base = {
            "schema_version": 1, "profile_version": "precision-v1",
            "source_hash": "source", "book": {"title": "T"},
            "concepts": [{
                "concept_id": "x", "source_terms": ["term"],
                "original_terms": [], "canonical_ko": "용어",
                "avoid_ko": [], "kind": "concept", "rationale": "reason",
                "strict": True,
            }],
        }
        a = {**base, "research": {"web_evidence": ["old ranking"]}}
        b = {**base, "research": {"web_evidence": ["new ranking"]}}
        self.assertEqual(precision_profile_hash(a), precision_profile_hash(b))

    def test_prompt_contains_rationale_and_forbidden_variant(self):
        rules = _concept_rules({"concepts": [{
            "source_terms": ["free spirit"], "original_terms": ["Freigeist"],
            "canonical_ko": "자유정신", "avoid_ko": ["자유사상가"],
            "rationale": "Freidenker와 구별", "strict": True,
        }]})
        self.assertIn("자유정신", rules)
        self.assertIn("자유사상가", rules)
        self.assertIn("Freidenker와 구별", rules)


class QualityAuditTests(unittest.TestCase):
    def test_violation_is_corrected_and_rechecked(self):
        book = TranslatedBook(
            title="Test", author="N",
            chapters=[TranslatedChapter(title="V", pairs=[
                ("The free spirit asks for reasons.", "자유사상가는 이유를 요구한다."),
            ])],
        )
        sheet = {"concepts": [{
            "concept_id": "freigeist", "source_terms": ["free spirit"],
            "canonical_ko": "자유정신", "avoid_ko": ["자유사상가"],
            "rationale": "technical concept", "strict": True,
        }]}
        self.assertEqual(1, len(find_violations(book, sheet)))

        def chat(messages):
            return json.dumps({"corrections": [{
                "chapter_index": 0, "paragraph_index": 0,
                "corrected_ko": "자유정신은 이유를 요구한다.",
            }]}, ensure_ascii=False)

        report = audit_and_correct(book, sheet, chat)
        self.assertTrue(report["passed"])
        self.assertEqual(0, len(find_violations(book, sheet)))
        self.assertIn("자유정신", book.chapters[0].pairs[0][1])

    def test_audit_preserves_inline_html_tags(self):
        book = TranslatedBook(
            title="Test", author="N",
            chapters=[TranslatedChapter(title="V", pairs=[
                ("The <i>free spirit</i> asks.",
                 "<i>자유사상가</i>는 묻는다."),
            ])],
        )
        sheet = {"concepts": [{
            "concept_id": "freigeist", "source_terms": ["free spirit"],
            "canonical_ko": "자유정신", "avoid_ko": ["자유사상가"],
            "rationale": "technical concept", "strict": True,
        }]}

        def chat(messages):
            payload = json.loads(messages[1]["content"])
            stashed = payload["violations"][0]["current_ko"]
            self.assertIn("◀Q0TAG0▶", stashed)
            return json.dumps({"corrections": [{
                "chapter_index": 0, "paragraph_index": 0,
                "corrected_ko": "◀Q0TAG0▶자유정신◀Q0TAG1▶은 묻는다.",
            }]}, ensure_ascii=False)

        audit_and_correct(book, sheet, chat)
        self.assertEqual("<i>자유정신</i>은 묻는다.",
                         book.chapters[0].pairs[0][1])

    def test_translation_checkpoint_is_bound_to_precision_profile(self):
        sheet = {
            "profile_hash": "researched-profile",
            "concepts": [{
                "concept_id": "freigeist", "source_terms": ["free spirit"],
                "original_terms": ["Freigeist"], "canonical_ko": "자유정신",
                "avoid_ko": ["자유사상가"], "rationale": "technical concept",
                "strict": True,
            }],
        }
        replies = iter([
            "[1] 자유정신은 이유를 요구한다.",
            "[1] 자유정신은 이유를 요구한다.",
        ])
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint = os.path.join(temp_dir, "book.json")
            with patch("translate._chat", side_effect=lambda messages: next(replies)):
                book = translate_book(
                    "Test", "N",
                    [Chapter(title="V", paragraphs=[
                        "The free spirit asks for reasons.",
                    ])],
                    checkpoint_path=checkpoint, max_workers=1,
                    glossary={"free spirit": "자유정신"},
                    concept_sheet=sheet,
                )
            with open(checkpoint, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertTrue(book.quality_report["passed"])
            self.assertEqual(_quality_profile_fingerprint(sheet),
                             saved["quality_profile_hash"])
            self.assertEqual("자유정신은 이유를 요구한다.",
                             saved["chapters"][0]["pairs"][0][1])


if __name__ == "__main__":
    unittest.main()
