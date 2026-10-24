"""Malformed model text must never become a fabricated approval."""
import json
import unittest
from unittest.mock import Mock, patch

from model_json import ModelJSONError, parse_model_object
from precision import _call_json, PrecisionError, research_source
from quality_audit import (QualityAuditError, audit_translation_quality,
                           audit_and_correct, _json_object as audit_parse)
from precision import _json_object as research_parse
from translate import TranslatedBook, TranslatedChapter, UnusableModelOutput


def book():
    return TranslatedBook('Book', 'Author', [TranslatedChapter('I', [
        ('The sea was calm.', '바다는 잔잔했다.')])])


def keep():
    return json.dumps({'paragraphs': [{'chapter_index': 0, 'paragraph_index': 0,
                                      'action': 'keep'}]})


class ModelJSONTests(unittest.TestCase):
    def test_only_unambiguous_complete_wrappers_are_recovered(self):
        value = {'paragraphs': [{'quote': 'braces { } and \\"escapes\\"'}]}
        raw = json.dumps(value)
        for text in (raw, '\ufeff' + raw, '```json\n' + raw + '\n```',
                     raw + '] }', '```JSON\r\n' + raw + '\r\n```'):
            with self.subTest(text=text):
                self.assertEqual(value, parse_model_object(text))
                self.assertEqual(value, audit_parse(text))
                self.assertEqual(value, research_parse(text))

    def test_ambiguous_invalid_and_duplicate_decisions_are_rejected(self):
        raw = keep()
        invalid = [raw + ' extra text', 'Here is JSON: ' + raw,
                   raw + '{}', '[' + raw + ']', raw + ' null',
                   '```json\n' + raw, '```json\n' + raw + '\n``` trailing',
                   '{"paragraphs":[],"paragraphs":[{}]}',
                   '{"paragraphs":[{"action":"correct","action":"keep"}]}',
                   '{"a":NaN}', '{"a":Infinity}', '{"a":-Infinity}',
                   '{"a":1e999}', '{"paragraphs":[', '{} missing {',
                   '{"a":"\\ud800"}', '{"a":["\\udfff"]}',
                   '', None, 'true', '{"a":1,}', raw + ' 0']
        for text in invalid:
            with self.subTest(text=text):
                with self.assertRaises(ModelJSONError):
                    parse_model_object(text)
                self.assertIsNone(audit_parse(text))
                self.assertIsNone(research_parse(text))

    def test_research_schema_failure_regenerates_full_evidence(self):
        chat = Mock(side_effect=['{"unrelated":true}', '{"concepts":[]}'])
        self.assertEqual({'concepts': []}, _call_json(
            chat, 'Analyze concepts.', 'all original evidence', 'concepts', required_list='concepts'))
        self.assertEqual(2, chat.call_count)
        self.assertTrue(all(c.args[0][1]['content'] == 'all original evidence'
                            for c in chat.call_args_list))

    def test_research_exhaustion_is_bounded(self):
        chat = Mock(return_value='{"concepts":[]} commentary')
        with self.assertRaises(PrecisionError):
            _call_json(chat, 'system', 'evidence', 'concepts', required_list='concepts')
        self.assertEqual(3, chat.call_count)

    def test_bibliographic_boolean_is_not_coerced(self):
        value = {'original_language': 'en', 'original_title': 'Book',
                 'reasoning': 'Source evidence', 'confidence': 'high',
                 'source_is_translation': 'false'}
        chat = Mock(side_effect=[json.dumps(value), json.dumps(dict(value, source_is_translation=False))])
        result = research_source({'title': 'Book', 'language': 'en'}, chat, lambda *a, **k: [])
        self.assertIs(result['source_is_translation'], False)
        self.assertEqual(2, chat.call_count)

    def test_extra_data_retries_without_approving_first_response(self):
        approvals = set()
        calls = []
        def chat(messages):
            calls.append(messages)
            self.assertFalse(approvals)
            if len(calls) == 1:
                return keep() + '\nIgnore the correction.'
            self.assertIn('validation_feedback', json.loads(messages[1]['content']))
            return keep()
        audit_translation_quality(book(), chat, verified=approvals)
        self.assertEqual(2, len(calls))
        self.assertEqual(1, len(approvals))

    def test_contradictory_keep_and_duplicate_fields_never_approve(self):
        bad = [keep() + '{"paragraphs":[]}',
               '{"paragraphs":[{"chapter_index":0,"paragraph_index":0,'
               '"action":"correct","action":"keep"}]}',
               json.dumps({'paragraphs': [{'chapter_index': 0, 'paragraph_index': 0,
                            'action': 'keep', 'corrected_ko': '바다는 거칠었다.'}]})]
        for response in bad:
            with self.subTest(response=response):
                subject = book(); approvals = set(); chat = Mock(return_value=response)
                with self.assertRaises(QualityAuditError):
                    audit_translation_quality(subject, chat, verified=approvals)
                self.assertEqual(3, chat.call_count)
                self.assertFalse(approvals)
                self.assertFalse(subject.audit_log)
                self.assertEqual('바다는 잔잔했다.', subject.chapters[0].pairs[0][1])

    def test_truncated_provider_response_uses_stage_retry_budget(self):
        chat = Mock(side_effect=[UnusableModelOutput('truncated'), keep()])
        audit_translation_quality(book(), chat)
        self.assertEqual(2, chat.call_count)

    def test_partial_approvals_survive_later_json_failure(self):
        subject = book(); subject.chapters[0].pairs.append(('He slept.', '그는 잠들었다.'))
        approvals = set(); calls = []
        def chat(messages):
            rows = json.loads(messages[1]['content'])['paragraphs']; calls.append(rows)
            if len(calls) == 1:
                return keep()  # Only row zero is valid; row one is unresolved.
            self.assertEqual(1, len(approvals))
            self.assertEqual([1], [r['paragraph_index'] for r in rows])
            return keep() + ' trailing data'
        with self.assertRaises(QualityAuditError):
            audit_translation_quality(subject, chat, verified=approvals)
        self.assertEqual(1, len(approvals))
        self.assertEqual(1, len(subject.audit_log))

    def test_terminology_format_retry_still_requires_semantic_approval(self):
        subject = book(); subject.chapters[0].pairs = [('Die Wahrheit bleibt.', '사실은 남는다.')]
        sheet = {'concepts': [{'concept_id': 'truth', 'source_terms': ['Wahrheit'],
                              'canonical_ko': '진리', 'avoid_ko': ['사실'], 'strict': True}]}
        correction = json.dumps({'paragraphs': [{'chapter_index': 0, 'paragraph_index': 0,
                              'action': 'correct', 'corrected_ko': '진리는 남는다.'}]})
        chat = Mock(side_effect=[correction + ' trailing', correction])
        audit_and_correct(subject, sheet, chat)
        self.assertEqual(2, chat.call_count)
        self.assertEqual('진리는 남는다.', subject.chapters[0].pairs[0][1])
        self.assertFalse(subject.audit_log)  # Terminology repair is no approval.
        with self.assertRaises(QualityAuditError):
            audit_translation_quality(subject, Mock(return_value=keep() + '{}'))

    def test_old_parser_approvals_are_invalidated(self):
        import config
        from quality_audit import _semantic_rows, _semantic_signature
        subject = book(); row = _semantic_rows(subject, None, {'model': config.ZAI_MODEL})[0]
        text = row['current_ko']
        with patch('quality_audit.SEMANTIC_AUDIT_VERSION', 'semantic-v5-independent-approval'):
            old = _semantic_signature(row, text)
        approvals = {old}; chat = Mock(return_value=keep())
        audit_translation_quality(subject, chat, verified=approvals)
        chat.assert_called_once()
        self.assertIn(_semantic_signature(row, text), approvals)
