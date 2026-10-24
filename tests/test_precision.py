from __future__ import annotations

import json
import os
import tempfile
import unittest

from extract import Chapter
from precision import (_call_json, _cohesive_source_forms,
                       _reconcile_lexical_targets, _validate_concepts,
                       _vet_related_candidates, build_concept_sheet,
                       choose_original_candidate, _related_lexical_candidates,
                       extract_repeated_phrases, flat_glossary,
                       precision_profile_hash)
from quality_audit import (QualityAuditError, audit_and_correct,
                           audit_translation_quality,
                           find_translation_integrity_violations,
                           find_violations)
from translate import (TranslatedBook, TranslatedChapter, _concept_rules,
                       _quality_profile_fingerprint,
                       _remap_structural_checkpoint, translate_book)
from unittest.mock import patch


class PrecisionResearchTests(unittest.TestCase):
    def test_greek_inflected_forms_do_not_crash_or_disappear(self):
        forms = _cohesive_source_forms(['Ὀδυσσεύς', 'Ὀδυσῆος'])
        self.assertTrue(forms)
        self.assertTrue(set(forms) <= {'Ὀδυσσεύς', 'Ὀδυσῆος'})
        self.assertEqual(['ἦ', 'ἦ'], _cohesive_source_forms(['ἦ', 'ἦ']))

    def test_malformed_json_regenerates_original_evidence(self):
        replies = iter(['{"concepts":[{"broken":',
                        '{"concepts":[{"fixed":true}]}'])
        calls = []

        def chat(messages):
            calls.append(messages)
            return next(replies)

        parsed = _call_json(chat, "system", "user", "test stage")
        self.assertTrue(parsed["concepts"][0]["fixed"])
        self.assertEqual(2, len(calls))
        self.assertIn("Fresh JSON regeneration", calls[1][0]["content"])
        self.assertEqual("user", calls[1][1]["content"])
        self.assertNotIn("broken", calls[1][1]["content"])

    def test_empty_json_response_regenerates_original_task(self):
        replies = iter(["", '{"concepts":[{"regenerated":true}]}'])
        calls = []

        def chat(messages):
            calls.append(messages)
            return next(replies)

        parsed = _call_json(chat, "system", "original payload", "test stage")
        self.assertTrue(parsed["concepts"][0]["regenerated"])
        self.assertEqual(2, len(calls))
        self.assertIn("Fresh JSON regeneration", calls[1][0]["content"])
        self.assertEqual("original payload", calls[1][1]["content"])

    def test_permanent_parameter_error_is_not_replayed(self):
        calls = []
        def chat(messages):
            calls.append(messages)
            raise RuntimeError("Z.ai HTTP 400 business_code=1213")
        with self.assertRaisesRegex(RuntimeError, "1213"):
            _call_json(chat, "system", "complete evidence", "normalization")
        self.assertEqual(1, len(calls))

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

    def test_thinker_role_scan_does_not_require_freigeist_anchor(self):
        text = (
            "freie Geister freie Geister freie Geister. "
            "Ein jüdischer Freidenker erscheint nur einmal. "
            "Die Meinung des Denkers bleibt offen."
        )
        related = _related_lexical_candidates(text, [{
            "source_terms": ["freie Geister"],
            "original_terms": ["freier Geist"],
        }])
        terms = {row["source_terms"][0] for row in related}
        self.assertIn("Freidenker", terms)
        self.assertNotIn("Denkers", terms)

    def test_thinker_role_scan_has_priority_in_bounded_candidates(self):
        text = (
            "Freigeist Freigeist Freigeist. "
            + " ".join(f"Freigxxxx{i}" for i in range(20))
            + " Ein Freidenker widerspricht."
        )
        related = _related_lexical_candidates(text, [{
            "source_terms": ["Freigeist"],
            "original_terms": ["Freigeist"],
        }], limit=1)
        self.assertEqual("Freidenker", related[0]["source_terms"][0])

    def test_different_head_nouns_cannot_share_strict_mapping(self):
        forms = _cohesive_source_forms([
            "freier Geist", "freie Geister", "Freigeist", "Freigeister",
            "Freigesinnten", "freien Geister", "freien Willen",
        ])
        self.assertIn("Freigeist", forms)
        self.assertIn("freien Geister", forms)
        self.assertNotIn("freien Willen", forms)
        self.assertNotIn("Freigesinnten", forms)

    def test_base_lemma_excludes_derivational_form_from_strict_mapping(self):
        text = (
            "Der freier Geist und freie Geister heißen auch Freigeist und "
            "Freigeister, aber Freigeisterei ist eine eigene Bildung."
        )
        concepts = _validate_concepts([{
            "concept_id": "freigeist.forms",
            "source_terms": [
                "freier Geist", "freie Geister", "Freigeist",
                "Freigeister", "Freigeisterei",
            ],
            "original_terms": ["freier Geist", "Freigeist"],
            "canonical_ko": "자유정신",
            "confidence": "high",
            "strict": True,
        }], text, set())
        self.assertEqual([
            "freier Geist", "freie Geister", "Freigeist", "Freigeister",
        ], concepts[0]["source_terms"])

    def test_conflicting_inflectional_targets_follow_higher_confidence(self):
        concepts = _reconcile_lexical_targets([{
            "concept_id": "freier.geist",
            "source_terms": ["freier Geist", "freie Geister"],
            "original_terms": ["freier Geist"],
            "canonical_ko": "자유로운 정신",
            "avoid_ko": ["자유사상가"],
            "confidence": "high",
        }, {
            "concept_id": "freigeist.inflection",
            "source_terms": ["Freigeister"],
            "original_terms": ["Freigeist"],
            "canonical_ko": "자유사상가",
            "avoid_ko": ["자유로운 정신"],
            "confidence": "medium",
        }])
        self.assertEqual("자유로운 정신", concepts[1]["canonical_ko"])
        self.assertIn("자유사상가", concepts[1]["avoid_ko"])
        self.assertNotIn("자유로운 정신", concepts[1]["avoid_ko"])

    def test_neighbor_screening_preserves_distinct_freethinker(self):
        candidates = [{
            "source_terms": ["Freidenker"], "original_terms": ["Freidenker"],
            "kind": "related_candidate",
        }, {
            "source_terms": ["Freilich"], "original_terms": ["Freilich"],
            "kind": "related_candidate",
        }]

        def chat(messages):
            # Even if the screening model misses it, a distinct thinker-role
            # lemma remains mandatory for the later evidence-based adjudication.
            return json.dumps({"candidates": []})

        selected = _vet_related_candidates(
            candidates,
            "Der Freidenker widerspricht. Freilich ist dies schwer.",
            "Menschliches", "Nietzsche", chat,
        )
        self.assertEqual(["Freidenker"], selected[0]["source_terms"])
        self.assertEqual(1, len(selected))

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
        }, {
            "concept_id": "freethinker",
            "source_terms": ["free-thinker"],
            "original_terms": ["Freidenker"],
            "canonical_ko": "자유사상가",
            "avoid_ko": ["자유정신"],
            "kind": "concept",
            "rationale": "Freigeist와 구별되는 사상가 유형",
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
            if len(calls) == 2:
                return json.dumps({"candidates": [{
                    "source_terms": ["free-thinker"],
                }]})
            return json.dumps({"concepts": final_concepts}, ensure_ascii=False)

        def search(query, count=4):
            return [{"title": "Primary text", "link": "https://example.org/primary",
                     "content": "Freigeist in the critical text"}]

        sheet = build_concept_sheet(
            chapters, "Human, All Too Human", "Nietzsche", "en",
            {"source_is_translation": True, "original_language": "de"},
            chat, search)
        self.assertEqual(5, len(calls))
        self.assertEqual("자유정신", flat_glossary(sheet)["free spirit"])
        self.assertEqual("자유사상가", flat_glossary(sheet)["free-thinker"])
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
    def test_avoided_prefix_inside_canonical_is_not_a_violation(self):
        sheet = {"concepts": [{
            "concept_id": "troy", "source_terms": ["Τροίην"],
            "canonical_ko": "트로이아", "avoid_ko": ["트로이"],
            "strict": True,
        }]}
        book = TranslatedBook(title="Test", author="Homer", chapters=[
            TranslatedChapter(title="I", pairs=[("Τροίην", "트로이아로 갔다.")])])
        self.assertEqual([], find_violations(book, sheet))
        book.chapters[0].pairs[0] = ("Τροίην", "트로이아와 트로이로 갔다.")
        self.assertEqual(["트로이"], find_violations(book, sheet)[0]["avoid_ko_found"])

        def chat(messages):
            return json.dumps({"corrections": [{
                "chapter_index": 0, "paragraph_index": 0,
                "corrected_ko": "트로이아로 갔다.",
            }]}, ensure_ascii=False)

        self.assertTrue(audit_and_correct(book, sheet, chat)["passed"])
        self.assertEqual([], find_violations(book, sheet))

    def test_whole_book_audit_repairs_untranslated_quote_and_korean_typo(self):
        book = TranslatedBook(
            title="Test", author="N",
            chapters=[TranslatedChapter(title="V", pairs=[
                ("Sorrow is knowledge, and knowledge wounds.",
                 "Sorrow is knowledge, and knowledge wounds."),
                ('"Unmoralisch" bezeichnet einen Zurückgebliebenen.',
                 '"비도델적"은 뒤처진 자를 가리킨다.'),
            ])],
        )

        def chat(messages):
            payload = json.loads(messages[1]["content"])
            self.assertEqual(2, len(payload["paragraphs"]))
            if payload["paragraphs"][0]["current_ko"].startswith("슬픔"):
                return json.dumps({"paragraphs": [
                    {"chapter_index": 0, "paragraph_index": i, "action": "keep"}
                    for i in range(2)]})
            return json.dumps({"paragraphs": [{
                "chapter_index": 0, "paragraph_index": 0,
                "action": "correct",
                "corrected_ko": "슬픔은 인식이며, 인식은 상처를 입힌다.",
                "reason": "영어 인용문이 미번역 상태다.",
                "source_quote": "Sorrow is knowledge, and knowledge wounds.",
            }, {
                "chapter_index": 0, "paragraph_index": 1,
                "action": "correct",
                "corrected_ko": '"비도덕적"은 뒤처진 자를 가리킨다.',
                "reason": "한국어 오탈자를 바로잡았다.", "category": "korean",
            }]}, ensure_ascii=False)

        report = audit_translation_quality(book, chat, chunk_size=2)
        self.assertEqual(2, report["semantic_paragraphs_corrected"])
        self.assertEqual([], find_translation_integrity_violations(book))
        self.assertIn("비도덕적", book.chapters[0].pairs[1][1])

    def test_whole_book_audit_rejects_kept_untranslated_passage(self):
        book = TranslatedBook(
            title="Test", author="N",
            chapters=[TranslatedChapter(title="V", pairs=[
                ("Sorrow is knowledge, and knowledge wounds.",
                 "Sorrow is knowledge, and knowledge wounds."),
            ])],
        )

        def chat(messages):
            return json.dumps({"paragraphs": [{
                "chapter_index": 0, "paragraph_index": 0,
                "action": "keep", "reason": "incorrect keep",
            }]})

        with self.assertRaisesRegex(QualityAuditError, "failed closed"):
            audit_translation_quality(book, chat, chunk_size=1)

    def test_distinct_source_lemma_can_contextually_exempt_avoided_korean(self):
        book = TranslatedBook(
            title="Test", author="N",
            chapters=[TranslatedChapter(title="V", pairs=[
                (
                    "Der Nutzen der Erkenntniss und die Bereicherung des Wissens.",
                    "인식의 유용성과 지식의 풍부함.",
                ),
                (
                    "Die moderne Cultur und der Sieg der Bildung.",
                    "근대 문화와 교양의 승리.",
                ),
            ])],
        )
        sheet = {"concepts": [{
            "concept_id": "c10", "source_terms": ["Erkenntniss"],
            "canonical_ko": "인식", "avoid_ko": ["지식"],
            "rationale": "Wissen과 구별", "strict": True,
        }, {
            "concept_id": "c03", "source_terms": ["Cultur"],
            "canonical_ko": "문화", "avoid_ko": ["교양"],
            "rationale": "Bildung과 구별", "strict": True,
        }]}
        self.assertEqual(2, len(find_violations(book, sheet)))

        def chat(messages):
            payload = json.loads(messages[1]["content"])
            self.assertEqual(2, len(payload["paragraphs"]))
            return json.dumps({"paragraphs": [{
                "chapter_index": 0, "paragraph_index": 0,
                "action": "keep",
                "corrected_ko": "인식의 유용성과 지식의 풍부함.",
                "contextual_exemptions": [{
                    "concept_id": "c10", "avoid_ko": "지식",
                    "source_expression": "Wissens",
                    "reason": "지식은 별도 원어 Wissen의 번역이다.",
                }],
            }, {
                "chapter_index": 0, "paragraph_index": 1,
                "action": "keep",
                "corrected_ko": "근대 문화와 교양의 승리.",
                "contextual_exemptions": [{
                    "concept_id": "c03", "avoid_ko": "교양",
                    "source_expression": "Bildung",
                    "reason": "교양은 별도 원어 Bildung의 번역이다.",
                }],
            }]}, ensure_ascii=False)

        report = audit_and_correct(book, sheet, chat, chunk_size=2)
        self.assertTrue(report["passed"])
        self.assertEqual(2, report["contextual_exemptions"])
        self.assertEqual(0, report["paragraphs_corrected"])

    def test_contextual_exemption_cannot_hide_missing_canonical_term(self):
        book = TranslatedBook(
            title="Test", author="N",
            chapters=[TranslatedChapter(title="V", pairs=[
                ("Die Erkenntniss wächst.", "지식이 자란다."),
            ])],
        )
        sheet = {"concepts": [{
            "concept_id": "c10", "source_terms": ["Erkenntniss"],
            "canonical_ko": "인식", "avoid_ko": ["지식"],
            "rationale": "strict epistemological term", "strict": True,
        }]}

        def chat(messages):
            return json.dumps({"paragraphs": [{
                "chapter_index": 0, "paragraph_index": 0,
                "action": "keep", "corrected_ko": "지식이 자란다.",
                "contextual_exemptions": [{
                    "concept_id": "c10", "avoid_ko": "지식",
                    "source_expression": "Erkenntniss",
                    "reason": "잘못된 예외 시도",
                }],
            }]}, ensure_ascii=False)

        with self.assertRaisesRegex(QualityAuditError, "reviewed 0/1"):
            audit_and_correct(book, sheet, chat)

    def test_nonconceptual_idiom_can_exempt_source_sense(self):
        book = TranslatedBook(
            title="Test", author="N",
            chapters=[TranslatedChapter(title="V", pairs=[
                ("Die Reise geschah in Wahrheit aus Abenteuerlust.",
                 "그 여행은 실은 모험심 때문에 이루어졌다."),
            ])],
        )
        sheet = {"concepts": [{
            "concept_id": "c05", "source_terms": ["Wahrheit"],
            "canonical_ko": "진리", "avoid_ko": ["참됨"],
            "rationale": "epistemological concept", "strict": True,
        }]}

        def chat(messages):
            return json.dumps({"paragraphs": [{
                "chapter_index": 0, "paragraph_index": 0,
                "action": "keep",
                "corrected_ko": "그 여행은 실은 모험심 때문에 이루어졌다.",
                "contextual_exemptions": [],
                "source_sense_exemptions": [{
                    "concept_id": "c05",
                    "source_expression": "in Wahrheit",
                    "reason": "여기서는 철학 개념이 아니라 '실은'이라는 관용구다.",
                }],
            }]}, ensure_ascii=False)

        report = audit_and_correct(book, sheet, chat)
        self.assertTrue(report["passed"])
        self.assertEqual(1, report["source_sense_exemptions"])
        self.assertEqual(0, report["paragraphs_corrected"])

    def test_standalone_concept_cannot_claim_source_sense_exemption(self):
        book = TranslatedBook(
            title="Test", author="N",
            chapters=[TranslatedChapter(title="V", pairs=[
                ("Die Wahrheit bleibt.", "사실은 남는다."),
            ])],
        )
        sheet = {"concepts": [{
            "concept_id": "c05", "source_terms": ["Wahrheit"],
            "canonical_ko": "진리", "avoid_ko": [], "strict": True,
        }]}

        def chat(messages):
            return json.dumps({"paragraphs": [{
                "chapter_index": 0, "paragraph_index": 0,
                "action": "keep", "corrected_ko": "사실은 남는다.",
                "contextual_exemptions": [],
                "source_sense_exemptions": [{
                    "concept_id": "c05", "source_expression": "Wahrheit",
                    "reason": "부당한 독립 용어 예외",
                }],
            }]}, ensure_ascii=False)

        with self.assertRaisesRegex(QualityAuditError, "reviewed 0/1"):
            audit_and_correct(book, sheet, chat)

    def test_new_forbidden_synonym_is_discarded_after_complete_classification(self):
        book = TranslatedBook(
            title="Test", author="N",
            chapters=[TranslatedChapter(title="V", pairs=[
                (
                    "Erkenntniss und Wissen.",
                    "인식과 지식.",
                ),
            ])],
        )
        sheet = {"concepts": [{
            "concept_id": "c10", "source_terms": ["Erkenntniss"],
            "canonical_ko": "인식", "avoid_ko": ["지식", "앎"],
            "rationale": "Wissen과 구별", "strict": True,
        }]}
        calls = []

        def chat(messages):
            payload = json.loads(messages[1]["content"])
            calls.append(payload)
            if len(calls) == 1:
                # The reviewer removed the original avoided word but introduced
                # a different forbidden synonym without exempting that literal.
                return json.dumps({"paragraphs": [{
                    "chapter_index": 0, "paragraph_index": 0,
                    "action": "correct", "corrected_ko": "인식과 앎.",
                    "contextual_exemptions": [{
                        "concept_id": "c10", "avoid_ko": "지식",
                        "source_expression": "Wissen",
                        "reason": "Wissen의 번역",
                    }],
                }]}, ensure_ascii=False)
            self.assertIn("validation_feedback", payload)
            return json.dumps({"paragraphs": [{
                "chapter_index": 0, "paragraph_index": 0,
                "action": "keep", "corrected_ko": "인식과 지식.",
                "contextual_exemptions": [{
                    "concept_id": "c10", "avoid_ko": "지식",
                    "source_expression": "Wissen",
                    "reason": "지식은 별도 원어 Wissen의 번역이다.",
                }],
            }]}, ensure_ascii=False)

        report = audit_and_correct(book, sheet, chat)
        self.assertTrue(report["passed"])
        self.assertEqual(1, len(calls))
        self.assertEqual("인식과 지식.", book.chapters[0].pairs[0][1])

    def test_correction_cannot_break_another_matched_strict_concept(self):
        book = TranslatedBook(
            title="Test", author="N",
            chapters=[TranslatedChapter(title="V", pairs=[
                ("Erkenntniss und Cultur.", "인식과 교양."),
            ])],
        )
        sheet = {"concepts": [{
            "concept_id": "c10", "source_terms": ["Erkenntniss"],
            "canonical_ko": "인식", "avoid_ko": ["지식", "앎"],
            "strict": True,
        }, {
            "concept_id": "c03", "source_terms": ["Cultur"],
            "canonical_ko": "문화", "avoid_ko": ["교양"],
            "strict": True,
        }]}
        calls = []

        def chat(messages):
            payload = json.loads(messages[1]["content"])
            calls.append(payload)
            self.assertEqual({"c03", "c10"}, {
                rule["concept_id"] for rule in payload["rules"]
            })
            if len(calls) == 1:
                return json.dumps({"paragraphs": [{
                    "chapter_index": 0, "paragraph_index": 0,
                    "action": "correct", "corrected_ko": "앎과 문화.",
                    "contextual_exemptions": [],
                    "source_sense_exemptions": [],
                }]}, ensure_ascii=False)
            return json.dumps({"paragraphs": [{
                "chapter_index": 0, "paragraph_index": 0,
                "action": "correct", "corrected_ko": "인식과 문화.",
                "contextual_exemptions": [],
                "source_sense_exemptions": [],
            }]}, ensure_ascii=False)

        report = audit_and_correct(book, sheet, chat)
        self.assertTrue(report["passed"])
        self.assertEqual(2, len(calls))
        self.assertEqual("인식과 문화.", book.chapters[0].pairs[0][1])

    def test_complete_context_classification_preserves_original_paragraph(self):
        book = TranslatedBook(
            title="Test", author="N",
            chapters=[TranslatedChapter(title="V", pairs=[
                ("Erkenntniss, Cultur und Bildung.", "인식, 문화와 교양."),
            ])],
        )
        sheet = {"concepts": [{
            "concept_id": "c10", "source_terms": ["Erkenntniss"],
            "canonical_ko": "인식", "avoid_ko": ["앎"], "strict": True,
        }, {
            "concept_id": "c03", "source_terms": ["Cultur"],
            "canonical_ko": "문화", "avoid_ko": ["교양"], "strict": True,
        }]}

        def chat(messages):
            # Even if the reviewer needlessly rewrites another valid concept,
            # a complete separate-lemma classification makes this a keep-only
            # decision and the original paragraph must win.
            return json.dumps({"paragraphs": [{
                "chapter_index": 0, "paragraph_index": 0,
                "action": "correct", "corrected_ko": "앎, 문화와 교양.",
                "contextual_exemptions": [{
                    "concept_id": "c03", "avoid_ko": "교양",
                    "source_expression": "Bildung",
                    "reason": "교양은 별도 원어 Bildung의 번역이다.",
                }],
                "source_sense_exemptions": [],
            }]}, ensure_ascii=False)

        report = audit_and_correct(book, sheet, chat)
        self.assertTrue(report["passed"])
        self.assertEqual(0, report["paragraphs_corrected"])
        self.assertEqual("인식, 문화와 교양.", book.chapters[0].pairs[0][1])

    def test_multiple_concepts_in_one_paragraph_are_applied_once(self):
        book = TranslatedBook(
            title="Test", author="N",
            chapters=[TranslatedChapter(title="V", pairs=[
                ("Cultur und Erkenntniss.", "교양과 지식."),
            ])],
        )
        sheet = {"concepts": [{
            "concept_id": "c03", "source_terms": ["Cultur"],
            "canonical_ko": "문화", "avoid_ko": ["교양"], "strict": True,
        }, {
            "concept_id": "c10", "source_terms": ["Erkenntniss"],
            "canonical_ko": "인식", "avoid_ko": ["지식"], "strict": True,
        }]}

        def chat(messages):
            payload = json.loads(messages[1]["content"])
            self.assertEqual(1, len(payload["paragraphs"]))
            self.assertEqual(2, len(payload["paragraphs"][0]["violations"]))
            return json.dumps({"paragraphs": [{
                "chapter_index": 0, "paragraph_index": 0,
                "action": "correct", "corrected_ko": "문화와 인식.",
                "contextual_exemptions": [],
            }]}, ensure_ascii=False)

        report = audit_and_correct(book, sheet, chat)
        self.assertEqual(2, report["violations_found"])
        self.assertEqual(1, report["paragraphs_reviewed"])
        self.assertEqual(1, report["paragraphs_corrected"])
        self.assertEqual("문화와 인식.", book.chapters[0].pairs[0][1])

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
            stashed = payload["paragraphs"][0]["current_ko"]
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
            json.dumps({"paragraphs": [{
                "chapter_index": 0, "paragraph_index": 0,
                "action": "keep", "reason": "정확한 번역이다.",
            }]}, ensure_ascii=False),
        ])
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint = os.path.join(temp_dir, "book.json")
            with patch("translate._chat",
                       side_effect=lambda messages, **kwargs: next(replies)):
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

    def test_structural_checkpoint_remaps_exact_source_units(self):
        checkpoint = {
            "title": "Test", "author": "N", "source_lang": "de",
            "chapters": [{"title": "Untitled (Part 1/2)", "pairs": [
                ["Boilerplate", "보일러플레이트"],
                ["Erster\nTeil", "첫 부분"],
                ["Zweiter Teil", "둘째 부분"],
            ]}],
        }
        chapters = [
            Chapter(title="Eins", paragraphs=["Erster Teil"]),
            Chapter(title="Zwei", paragraphs=["Zweiter Teil"]),
        ]
        remapped = _remap_structural_checkpoint(
            checkpoint, chapters, "Test", "N", "de", minimum_coverage=1.0)
        self.assertIsNotNone(remapped)
        self.assertEqual("첫 부분", remapped[0][0][1])
        self.assertEqual("둘째 부분", remapped[1][0][1])


if __name__ == "__main__":
    unittest.main()
