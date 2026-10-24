"""A completed flag alone cannot authorize revision publication."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import config
from publication import PublicationError
from publish_reviewed import checked_review
from quality_audit import _semantic_rows, _semantic_signature
from review_library import VERSION, serialize, save, digest
from translate import TranslatedBook, TranslatedChapter
from precision import precision_profile_hash


class ReviewedPublicationTests(unittest.TestCase):
    def fixture(self,root):
        directory=root/'edition';directory.mkdir()
        source=root/'source.txt';source.write_text('A source sentence.')
        book=TranslatedBook('Title','Author',[TranslatedChapter('I',[('A source sentence.','원문 문장이다.')])])
        baseline={**serialize(book),'source_sha256':digest(source.read_bytes())}
        sheet={'concepts':[]};sheet['profile_hash']=precision_profile_hash(sheet)
        profile={'model':config.ZAI_MODEL,'quality_profile':sheet['profile_hash'],
                 'source_sha256':baseline['source_sha256'],'review_version':VERSION}
        verified=[_semantic_signature(r,r['current_ko']) for r in _semantic_rows(book,None,profile)]
        save(directory/'baseline.json',baseline)
        save(directory/'concepts.json',sheet)
        state={'stage':'completed','verified':verified,'book':serialize(book),
               'baseline_hash':digest((directory/'baseline.json').read_bytes())}
        save(directory/'checkpoint.json',state)
        output=root/'approved.epub';output.write_bytes(b'approved artifact')
        save(directory/'result.json',{'quality_report':{'passed':True},'output':str(output),'sha256':digest(output.read_bytes())})
        return {'id':'edition','source_path':str(source)},state,output

    def test_unapproved_target_is_rejected_despite_completed_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);entry,state,output=self.fixture(root)
            state['book']['chapters'][0]['pairs'][0]=['A source sentence.','바뀐 미승인 문장이다.']
            save(root/'edition/checkpoint.json',state)
            with patch('epub_validate.validate_epub') as validate:
                with self.assertRaises(PublicationError):checked_review(root,entry)
                validate.assert_not_called()

    def test_altered_artifact_rejected_before_validation_or_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);entry,state,output=self.fixture(root);output.write_bytes(b'altered')
            with self.assertRaises(PublicationError):checked_review(root,entry)

    def test_publication_compares_every_pair_to_approved_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);entry,state,output=self.fixture(root)
            with patch('epub_validate.validate_epub',return_value={'epubcheck':'passed'}) as validate:
                checked_review(root,entry)
                self.assertEqual([('A source sentence.','원문 문장이다.')],validate.call_args.kwargs['expected_content'])

    def test_changed_concepts_cannot_keep_old_approval_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);entry,state,output=self.fixture(root)
            p=root/'edition/concepts.json';sheet=json.loads(p.read_text())
            sheet['concepts']=[{'canonical_ko':'다른 번역','source_terms':['source']}]
            save(p,sheet)
            with self.assertRaisesRegex(ValueError,'profile changed'):checked_review(root,entry)

    def test_changed_baseline_and_source_cannot_pass_completed_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);entry,state,output=self.fixture(root)
            p=root/'edition/baseline.json';baseline=json.loads(p.read_text())
            baseline['chapters'][0]['pairs'][0][0]='A different source.'
            save(p,baseline)
            with self.assertRaisesRegex(ValueError,'baseline changed'):checked_review(root,entry)


if __name__=='__main__':unittest.main()
