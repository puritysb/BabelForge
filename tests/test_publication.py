"""Publication failure/cleanup ordering; no real server, network, or deletion."""
from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import catalog
import config
import publication as p
import publish


class PublicationTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);library=root/'library';library.mkdir()
            original=library/'new.epub';original.write_bytes(b'validated revision')
            old=library/'old.epub';old.write_bytes(b'obsolete')
            with patch.multiple(config,DATA_DIR=str(root),LIBRARY_DIR=str(library),
                                CHECKPOINT_DIR=str(root/'checkpoints'),CATALOG_PATH=str(root/'catalog.json')):
                req=catalog.new_request('Revision')
                catalog.select(req,{'source':'gutenberg','source_id':'1','language':'en'})
                previous=catalog.new_request('Previous')
                identifier=publish.edition_identifier(str(original),'gutenberg','1','en')
                catalog.update(previous,status='published',publication_identifier=identifier,
                               epub_path=str(old),artifact_sha256=p.digest(old))
                with patch.object(p,'content',return_value=[('Source','번역')]), \
                     patch.object(p,'validate_epub',return_value={'passed':True,'epubcheck':'passed'}), \
                     patch.object(p,'calibre_session',side_effect=lambda:nullcontext()), \
                     patch.object(publish,'_matching_ids',return_value=[7]), \
                     patch.object(publish,'add_to_library',return_value=7) as add, \
                     patch.object(p,'verify_public',return_value={'validation':{'passed':True}}) as verify, \
                     patch.object(p,'_run') as run:
                    yield original,old,req,previous,add,verify,run,root

    def call(self,original,req,**kwargs):
        return p.publish_revision(str(original),'Title','Author',source='gutenberg',source_id='1',
                                  source_lang='en',request_id=req,**kwargs)

    def test_verification_precedes_commit_and_deletion(self):
        with self.fixture() as (original,old,req,previous,add,verify,run,root):
            def check(*args):
                self.assertTrue(old.exists())
                self.assertNotEqual('published',catalog.get(req)['status'])
                with self.assertRaises(catalog.RequestAlreadyRunning):
                    catalog.claim_retry(req)
                return {'validation':{'passed':True}}
            verify.side_effect=check
            state=self.call(original,req)
            self.assertEqual('completed',state['stage'])
            self.assertFalse(old.exists())
            self.assertIsNone(catalog.get(previous))
            self.assertEqual(7,catalog.get(req)['calibre_id'])
            self.assertFalse((root/'backups').exists())
            self.assertEqual([original],list(original.parent.glob('*.epub')))

    def test_pipeline_owned_request_lock_is_reused_without_double_locking(self):
        with self.fixture() as (original,old,req,previous,add,verify,run,root):
            with catalog.request_worker(req):
                self.assertEqual('completed',self.call(original,req)['stage'])

    def test_failed_public_download_keeps_old_files_and_catalog(self):
        with self.fixture() as (original,old,req,previous,add,verify,run,root):
            verify.side_effect=RuntimeError('public download differs')
            with self.assertRaises(RuntimeError): self.call(original,req)
            self.assertTrue(old.exists())
            self.assertIsNotNone(catalog.get(previous))
            self.assertNotEqual('published',catalog.get(req)['status'])
            run.assert_not_called()
            verify.side_effect=None
            self.assertEqual('completed',self.call(original,req)['stage'])
            self.assertFalse(old.exists())

    def test_official_checker_unavailable_never_uploads(self):
        with self.fixture() as (original,old,req,previous,add,verify,run,root):
            with patch.object(p,'validate_epub',return_value={'epubcheck':'not_available'}):
                with self.assertRaises(p.PublicationError):self.call(original,req)
            add.assert_not_called();self.assertTrue(old.exists())

    def test_artifact_changed_after_pipeline_approval_never_uploads(self):
        with self.fixture() as (original,old,req,previous,add,verify,run,root):
            with self.assertRaises(p.PublicationError):
                self.call(original,req,expected_sha256='an-earlier-approved-hash')
            add.assert_not_called();self.assertTrue(old.exists())

    def test_explicit_external_input_is_imported_without_modifying_user_file(self):
        with self.fixture() as (original,old,req,previous,add,verify,run,root):
            external=root/'external.epub';original.rename(external)
            state=self.call(external,req)
            self.assertEqual(external.read_bytes(),Path(state['destination']).read_bytes())
            self.assertTrue(Path(state['destination']).is_relative_to(Path(config.LIBRARY_DIR)))

    def test_only_same_identifier_duplicates_removed_canonical_kept(self):
        with self.fixture() as (original,old,req,previous,add,verify,run,root):
            with patch.object(publish,'_matching_ids',return_value=[7,8]):
                state=self.call(original,req)
            self.assertEqual(7,state['book_id'])
            self.assertEqual(7,add.call_args.kwargs['preferred_id'])
            run.assert_called_once_with('remove','--permanent','8')
            self.assertEqual(2,verify.call_count)

    def test_changed_or_unowned_obsolete_files_fail_before_upload(self):
        with self.fixture() as (original,old,req,previous,add,verify,run,root):
            for obsolete in [{str(old):'wrong hash'},{str(root/'unrelated.epub'):'hash'},
                             {str(original):p.digest(original)}]:
                with self.assertRaises(p.PublicationError):self.call(original,req,obsolete_files=obsolete)
            add.assert_not_called();self.assertTrue(old.exists())

    def test_file_changed_during_verification_is_not_deleted(self):
        with self.fixture() as (original,old,req,previous,add,verify,run,root):
            def change(*args):old.write_bytes(b'another edition');return {'passed':True}
            verify.side_effect=change
            with self.assertRaises(p.PublicationError):self.call(original,req)
            self.assertEqual(b'another edition',old.read_bytes())
            self.assertEqual('failed',catalog.get(req)['status'])
            self.assertIsNotNone(catalog.get(previous))

    def test_cleanup_failure_resumes_same_request_before_marking_published(self):
        from publish_reviewed import publication_options
        with self.fixture() as (original,old,req,previous,add,verify,run,root):
            unlink=Path.unlink
            def deny_old(path,*args,**kwargs):
                if path==old:raise PermissionError('simulated cleanup failure')
                return unlink(path,*args,**kwargs)
            with patch.object(Path,'unlink',autospec=True,side_effect=deny_old):
                with self.assertRaises(PermissionError):self.call(original,req)
            self.assertEqual('failed',catalog.get(req)['status'])
            self.assertIsNotNone(catalog.get(previous));self.assertTrue(old.exists())
            options=publication_options({'inputs':[str(original)],'source_url':'https://www.gutenberg.org/ebooks/1'})
            self.assertEqual(req,options['request_id'])
            self.call(original,req)
            self.assertEqual('published',catalog.get(req)['status'])
            self.assertFalse(old.exists());self.assertIsNone(catalog.get(previous))

    def test_different_edition_with_same_title_is_untouched(self):
        with self.fixture() as (original,old,req,previous,add,verify,run,root):
            catalog.update(previous,publication_identifier='another-source-edition')
            self.call(original,req)
            self.assertTrue(old.exists());self.assertIsNotNone(catalog.get(previous))

    def test_unknown_request_and_shared_other_edition_file_fail_before_upload(self):
        with self.fixture() as (original,old,req,previous,add,verify,run,root):
            with self.assertRaises(p.PublicationError):self.call(original,'missing-request')
            shared=catalog.new_request('Other edition')
            catalog.update(shared,status='published',epub_path=str(old),publication_identifier='different')
            with self.assertRaises(p.PublicationError):self.call(original,req)
            add.assert_not_called();self.assertTrue(old.exists())

    def test_symlink_cleanup_rejected(self):
        with self.fixture() as (original,old,req,previous,add,verify,run,root):
            link=old.parent/'link.epub';link.symlink_to(old)
            with self.assertRaises(p.PublicationError):
                self.call(original,req,obsolete_files={str(link):p.digest(old)})
            self.assertTrue(old.exists());add.assert_not_called()

    def test_legacy_migration_retry_uses_bound_identity_without_old_artifact(self):
        with self.fixture() as (original,old,req,previous,add,verify,run,root):
            # The identity was bound on an earlier interrupted attempt. Its old
            # artifact is no longer an authority and must not prevent recovery.
            self.call(original,req,legacy_id=7,legacy_path=str(root/'gone.epub'),legacy_sha256='old')
            self.assertFalse(old.exists())

    def test_opds_pagination_and_exact_download_content(self):
        atom='http://www.w3.org/2005/Atom';origin=config.OPDS_BASE_URL
        feeds={config.OPDS_FEED_URL:f'<feed xmlns="{atom}"><entry><link href="/4f6e6577657374"/></entry></feed>'.encode(),
          origin+'/4f6e6577657374':f'<feed xmlns="{atom}"><link rel="next" href="/page2"/></feed>'.encode(),
          origin+'/page2':f'<feed xmlns="{atom}"><entry><link rel="http://opds-spec.org/acquisition" type="application/epub+zip" href="/get/epub/7/library"/></entry></feed>'.encode(),
          origin+'/get/epub/7/library':b'acquired',publish.opds_url_for(7):b'metadata differs'}
        with tempfile.TemporaryDirectory() as directory,patch.object(p,'fetch',side_effect=feeds.__getitem__),patch.object(p,'validate_epub',return_value={'epubcheck':'passed'}) as validate:
            p.verify_public(7,[('source','번역')],directory)
            self.assertEqual(2,validate.call_count)
            self.assertEqual([('source','번역')],validate.call_args.kwargs['expected_content'])

    def test_opds_wrong_origin_and_missing_record_fail(self):
        with self.assertRaises(p.PublicationError):p._same_origin('https://example.org','https://evil.example/get/epub/1','https://example.org')
        with tempfile.TemporaryDirectory() as directory,patch.object(p,'fetch',return_value=b'<feed xmlns="http://www.w3.org/2005/Atom"/>'):
            with self.assertRaises(p.PublicationError):p.verify_public(7,[],directory)

    def test_readonly_server_is_restarted_on_write_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'test.server.plist'
            path.write_bytes(plistlib.dumps({'Label':'test.server','ProgramArguments':[
                'calibre-server','--port','8080','--disable-local-write',config.CALIBRE_LIBRARY]}))
            loaded=[True]
            def launch_call(command,*args):
                if command=='print' and not loaded[0]:raise subprocess.CalledProcessError(3,['launchctl'])
                if command=='bootout':loaded[0]=False
                if command=='bootstrap':loaded[0]=True
            def connect(*args,**kw):
                if not loaded[0]:raise ConnectionRefusedError()
                return MagicMock()
            with patch.multiple(config,CALIBRE_LAUNCH_AGENT=str(path),DATA_DIR=directory), \
                 patch.object(p,'_launch',side_effect=launch_call) as launch, \
                 patch.object(p.socket,'create_connection',side_effect=connect):
                with self.assertRaisesRegex(RuntimeError,'write failed'):
                    with p.calibre_session():
                        self.assertEqual(str(Path(config.CALIBRE_LIBRARY).resolve()),publish.calibre_target.get())
                        raise RuntimeError('write failed')
                self.assertIsNone(publish.calibre_target.get())
                self.assertEqual('bootstrap',launch.call_args.args[0])
                self.assertFalse(p._maintenance_marker().exists())
            self.assertIn('--disable-local-write',plistlib.loads(path.read_bytes())['ProgramArguments'])

    def test_server_crash_marker_recovers_before_next_publication(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(config,'DATA_DIR',directory):
            info={'domain':'gui/1','label':'test','plist':'test.plist','port':8080,'host':'localhost'}
            p._write(str(p._maintenance_marker()),info)
            with patch.object(p,'_managed_server',return_value=info), \
                 patch.object(p,'_launch',side_effect=[subprocess.CalledProcessError(3,['launchctl']),None]) as launch, \
                 patch.object(p,'_wait_server'):
                p.recover_calibre_server()
            self.assertEqual('bootstrap',launch.call_args.args[0])
            self.assertFalse(p._maintenance_marker().exists())

    def test_unowned_stopped_server_is_never_automatically_started(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(config,'DATA_DIR',directory), \
             patch.object(p,'_launch') as launch,patch.object(p,'_managed_server',return_value={}):
            p.recover_calibre_server()
            launch.assert_not_called()

    def test_destination_and_request_cannot_belong_to_other_editions(self):
        with self.fixture() as (original,old,req,previous,add,verify,run,root):
            other=catalog.new_request('Other book')
            catalog.update(other,status='published',epub_path=str(original),publication_identifier='other')
            with self.assertRaises(p.PublicationError):self.call(original,req)
            add.assert_not_called()
            catalog.update(other,epub_path=None)
            catalog.update(req,publication_identifier='another-edition')
            with self.assertRaises(p.PublicationError):self.call(original,req)

    def test_reused_local_filename_cannot_overwrite_a_different_source(self):
        with self.fixture() as (original,old,req,previous,add,verify,run,root):
            source_id=str(root/'source.epub')
            identity=publish.edition_identifier(str(original),'local',source_id,'en')
            catalog.update(previous,publication_identifier=identity,source_sha256='old-source')
            catalog.select(req,{'source':'local','source_id':source_id,'language':'en'})
            with self.assertRaisesRegex(p.PublicationError,'another edition'):
                p.publish_revision(str(original),'Title','Author',source='local',source_id=source_id,
                    request_id=req,source_sha256='new-source')
            add.assert_not_called();self.assertTrue(old.exists())


if __name__=='__main__':unittest.main()
