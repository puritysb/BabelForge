"""Deployment gates use temporary books/state and mocked services only."""
from contextlib import contextmanager, ExitStack
import json
from pathlib import Path
import tempfile
import plistlib
import zipfile
import unittest
from unittest.mock import AsyncMock, patch

import babelforge_mcp
import catalog
import config
import deploy_mcp as d
import publication
import publish
from quality_audit import SEMANTIC_AUDIT_VERSION
from review_library import save
from state_store import file_lock
import test_publish_reviewed as reviewed


class DeploymentTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as mocks:
            root = Path(temporary); library = root/'library'; library.mkdir()
            mocks.enter_context(patch.multiple(config, DATA_DIR=str(root), LIBRARY_DIR=str(library),
                CATALOG_PATH=str(root/'catalog.json'), CHECKPOINT_DIR=str(root/'checkpoints')))
            entry, state, artifact = reviewed.ReviewedPublicationTests().fixture(root)
            result_path = root/'edition/result.json'; result = json.loads(result_path.read_text())
            result['quality_report']['semantic_audit_version'] = SEMANTIC_AUDIT_VERSION
            save(result_path, result)
            canonical = library/'current.epub'; canonical.write_bytes(artifact.read_bytes())
            req = catalog.new_request('Title')
            catalog.select(req, {'source': 'gutenberg', 'source_id': '1', 'language': 'en'})
            identifier = publish.edition_identifier(str(artifact), 'gutenberg', '1', 'en')
            catalog.update(req, status='published', publication_stage='completed',
                publication_identifier=identifier, calibre_id=13, opds_url=publish.opds_url_for(13),
                artifact_sha256=result['sha256'], epub_path=str(canonical))
            entry.update(language='en', publication={'source': 'gutenberg', 'source_id': '1',
                'request_id': req, 'destination': str(canonical)})
            manifest = root/'manifest.json'; save(manifest, {'review_dir': str(root), 'editions': [entry]})
            mocks.enter_context(patch('epub_validate.validate_epub', return_value={'epubcheck': 'passed'}))
            validate = mocks.enter_context(patch.object(d, 'validate_epub', return_value={'epubcheck': 'passed'}))
            listing = [{'id': 13, 'identifiers': {'babelforge': identifier}}]
            database = mocks.enter_context(patch.object(publish, '_run', side_effect=lambda *a, **k: json.dumps(listing)))
            feed = mocks.enter_context(patch.object(publication, 'opds_acquisitions', return_value={13: ['https://example.test/book']}))
            public = mocks.enter_context(patch.object(publication, 'verify_public', return_value={
                'validation': {'epubcheck': 'passed'}, 'stable_validation': {'epubcheck': 'passed'}}))
            mocks.enter_context(patch.object(d, 'git_state', return_value={'commit': 'abc', 'dirty': False}))
            mocks.enter_context(patch.object(d, 'managed_service', return_value={'target': 'test'}))
            pid = mocks.enter_context(patch.object(d, 'service_pid', return_value=111))
            def restarted(*args):
                pid.return_value = 222
                return 222
            restart = mocks.enter_context(patch.object(d, 'restart_service', side_effect=restarted))
            mcp = mocks.enter_context(patch.object(d, 'check_mcp', return_value={'tools': [], 'editions': 1}))
            yield locals()

    def report(self, f, **kwargs):
        return d.run(f['manifest'], report_path=f['root']/'report.json', **kwargs)

    def test_deployment_checks_before_restart_and_after_public_download(self):
        with self.fixture() as f:
            def verify(*args):
                self.assertEqual(1, f['restart'].call_count)
                journal = json.loads((f['root']/'report.json').read_text())
                self.assertFalse(journal['completed'])
                self.assertEqual('public_validation', journal['stage'])
                return {'validation': {'epubcheck': 'passed'}}
            f['public'].side_effect = verify
            report = self.report(f)
            self.assertTrue(report['completed']); self.assertEqual(1, report['approved_pairs'])
            self.assertEqual(222, report['pid']); self.assertEqual(3, f['mcp'].call_count)
            self.assertEqual([('A source sentence.', '원문 문장이다.')], f['public'].call_args.args[1])
            with file_lock(str(f['root']/'.deployment.lock'), blocking=False):
                pass  # success releases admission lock

    def test_check_only_never_restarts_or_changes_artifacts(self):
        with self.fixture() as f:
            before = f['canonical'].read_bytes()
            report = self.report(f, check_only=True)
            self.assertTrue(report['completed']); f['restart'].assert_not_called()
            self.assertEqual(before, f['canonical'].read_bytes())

    def test_stale_approval_blocks_restart_even_with_completed_flags(self):
        with self.fixture() as f:
            f['state']['verified'] = []; save(f['root']/'edition/checkpoint.json', f['state'])
            with self.assertRaises(publication.PublicationError): self.report(f)
            f['restart'].assert_not_called(); f['public'].assert_not_called()
            report = json.loads((f['root']/'report.json').read_text())
            self.assertFalse(report['completed']); self.assertIn('approval', report['error'])

    def test_canonical_epubcheck_required_before_restart(self):
        with self.fixture() as f:
            f['validate'].return_value = {'epubcheck': 'not_available'}
            with self.assertRaisesRegex(d.DeploymentError, 'EPUBCheck'): self.report(f)
            f['restart'].assert_not_called()

    def test_canonical_tampering_or_incomplete_publication_blocks_restart(self):
        for damage in ('artifact', 'stage'):
            with self.subTest(damage=damage), self.fixture() as f:
                if damage == 'artifact': f['canonical'].write_bytes(b'changed')
                else: catalog.update(f['req'], publication_stage='cleanup_pending')
                with self.assertRaises(d.DeploymentError): self.report(f)
                f['restart'].assert_not_called()

    def test_missing_published_request_in_manifest_is_not_silently_skipped(self):
        with self.fixture() as f:
            req = catalog.new_request('Unlisted'); catalog.update(req, status='published')
            with self.assertRaisesRegex(d.DeploymentError, 'all published'): self.report(f)

    def test_old_completed_catalog_alias_cannot_hide_behind_current_canonical_file(self):
        with self.fixture() as f:
            req = catalog.new_request('Old')
            catalog.update(req, status='completed', publication_identifier=f['identifier'],
                           epub_path=str(f['canonical']))
            with self.assertRaisesRegex(d.DeploymentError, 'Duplicate completed'): self.report(f)

    def test_delivery_bundle_requires_current_approved_bytes(self):
        for stale in (False, True):
            with self.subTest(stale=stale), self.fixture() as f:
                delivery = f['library']/'delivery'; delivery.mkdir()
                epub = delivery/'book.epub'; epub.write_bytes(f['artifact'].read_bytes())
                bundle = delivery/'books.zip'
                with zipfile.ZipFile(bundle, 'w') as archive:
                    archive.writestr('book.epub', b'old EPUB' if stale else epub.read_bytes())
                save(f['root']/'summary.json', {'completed': True, 'editions': [{
                    'id': 'edition', 'sha256': f['result']['sha256'], 'delivery_path': str(epub)}],
                    'bundle': str(bundle), 'bundle_sha256': publication.digest(bundle)})
                if stale:
                    with self.assertRaisesRegex(d.DeploymentError, 'Bundle contains an obsolete'): self.report(f)
                else: self.assertTrue(self.report(f)['completed'])

    def test_managed_launchagent_must_point_to_current_checkout_and_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'test.mcp.plist'
            settings = {'Label': path.stem, 'WorkingDirectory': config.BASE_DIR,
                        'ProgramArguments': [config.VENV_PYTHON, str(Path(config.BASE_DIR)/'babelforge_mcp.py'), '--http']}
            path.write_bytes(plistlib.dumps(settings))
            with patch.object(config, 'MCP_LAUNCH_AGENT', str(path)):
                self.assertEqual(str(path), d.managed_service()['plist'])
                settings['WorkingDirectory'] = '/another/checkout'; path.write_bytes(plistlib.dumps(settings))
                with self.assertRaisesRegex(d.DeploymentError, 'checkout/command mismatch'): d.managed_service()

    def test_git_commit_and_clean_checkout_are_required_for_restart(self):
        with patch.object(d.subprocess, 'check_output', side_effect=['abc', ' M deploy_mcp.py']):
            with self.assertRaisesRegex(d.DeploymentError, 'clean checkout'): d.git_state()
        with patch.object(d.subprocess, 'check_output', side_effect=['abc', '']):
            with self.assertRaisesRegex(d.DeploymentError, 'commit mismatch'): d.git_state('other')

    def test_extra_managed_calibre_copy_or_stale_opds_record_blocks_deployment(self):
        for problem in ('calibre', 'deleted', 'duplicate', 'missing'):
            with self.subTest(problem=problem), self.fixture() as f:
                if problem == 'calibre': f['listing'].append({'id': 14, 'identifiers': {'babelforge': f['identifier']}})
                elif problem == 'deleted': f['feed'].return_value[14] = ['old']
                elif problem == 'duplicate': f['feed'].return_value[13].append('duplicate')
                else: f['feed'].return_value.clear()
                with self.assertRaises(d.DeploymentError): self.report(f)
                f['restart'].assert_not_called()

    def test_unknown_local_copy_and_explicit_obsolete_deliverable_block_deployment(self):
        for problem in ('copy', 'bundle', 'explicit'):
            with self.subTest(problem=problem), self.fixture() as f:
                if problem == 'explicit':
                    old = f['root']/'old.epub'; old.write_bytes(b'old')
                    save(f['root']/'old-deliverables.json', [{'path': str(old)}])
                else:
                    old = f['library']/('duplicate.epub' if problem == 'copy' else 'old.zip')
                    old.write_bytes(f['canonical'].read_bytes())
                with self.assertRaises(d.DeploymentError): self.report(f)
                self.assertTrue(old.exists()); f['restart'].assert_not_called()

    def test_public_stale_download_records_failure_and_never_fakes_success(self):
        with self.fixture() as f:
            f['public'].side_effect = publication.PublicationError('download content differs')
            with self.assertRaises(publication.PublicationError): self.report(f)
            report = json.loads((f['root']/'report.json').read_text())
            self.assertFalse(report['completed']); self.assertEqual('public_validation', report['stage'])
            self.assertTrue(f['canonical'].exists())
            with self.assertRaisesRegex(d.DeploymentError, 'already exists'): self.report(f)

    def test_change_during_public_download_is_detected_by_final_recheck(self):
        with self.fixture() as f:
            def tamper(*args):
                f['canonical'].write_bytes(b'changed during download')
                return {'validation': {'epubcheck': 'passed'}}
            f['public'].side_effect = tamper
            with self.assertRaisesRegex(d.DeploymentError, 'changed'): self.report(f)

    def test_missing_manifest_still_records_failure_before_restart(self):
        with self.fixture() as f:
            f['manifest'].unlink()
            with self.assertRaises(FileNotFoundError): self.report(f)
            f['restart'].assert_not_called()
            report = json.loads((f['root']/'report.json').read_text())
            self.assertFalse(report['completed']); self.assertIn('FileNotFoundError', report['error'])

    def test_code_or_manifest_change_in_final_mcp_check_cannot_certify_completion(self):
        for changed in ('code', 'manifest', 'mcp'):
            with self.subTest(changed=changed), self.fixture() as f:
                def check(*args):
                    if f['mcp'].call_count == 3:
                        if changed == 'manifest': f['manifest'].write_text('{}')
                        elif changed == 'code': d.git_state.return_value = {'commit': 'changed', 'dirty': False}
                        else: raise d.DeploymentError('MCP stopped responding')
                    return {'tools': [], 'editions': 1}
                f['mcp'].side_effect = check
                with self.assertRaises(d.DeploymentError): self.report(f)
                self.assertFalse(json.loads((f['root']/'report.json').read_text())['completed'])

    def test_live_worker_or_pending_calibre_maintenance_blocks_restart(self):
        for problem in ('worker', 'maintenance'):
            with self.subTest(problem=problem), self.fixture() as f:
                if problem == 'worker':
                    lock = file_lock(catalog._worker_path(f['req']), blocking=False); lock.__enter__()
                    self.addCleanup(lock.__exit__, None, None, None)
                else: (f['root']/'.calibre-maintenance.json').write_text('{}')
                with self.assertRaises(d.DeploymentError): self.report(f)
                f['restart'].assert_not_called()

    def test_deployment_lock_rejects_mcp_new_and_retry_before_catalog_mutation(self):
        with self.fixture() as f, file_lock(str(f['root']/'.deployment.lock')):
            before = catalog._load()
            with patch.object(babelforge_mcp, '_spawn_pipeline') as spawn:
                self.assertIn('deployment', babelforge_mcp.translate_book({'title': 'Other'})['error'])
                self.assertIn('deployment', babelforge_mcp.retry_request(f['req'])['error'])
                spawn.assert_not_called()
            self.assertEqual(before, catalog._load())

    def test_restart_failure_has_failed_report_and_releases_locks(self):
        with self.fixture() as f:
            f['restart'].side_effect = RuntimeError('restart failed')
            with self.assertRaisesRegex(RuntimeError, 'restart failed'): self.report(f)
            report = json.loads((f['root']/'report.json').read_text())
            self.assertFalse(report['completed']); self.assertEqual('restarting', report['stage'])
            with file_lock(str(f['root']/'.publication.lock'), blocking=False): pass

    def test_interruption_during_restart_already_has_incomplete_stage_journal(self):
        with self.fixture() as f:
            def interrupted(*args):
                journal = json.loads((f['root']/'report.json').read_text())
                self.assertFalse(journal['completed']); self.assertEqual('restarting', journal['stage'])
                raise KeyboardInterrupt()
            f['restart'].side_effect = interrupted
            with self.assertRaises(KeyboardInterrupt): self.report(f)
            self.assertFalse(json.loads((f['root']/'report.json').read_text())['completed'])

    def test_mcp_readiness_retries_transport_only_and_rejects_stale_status_immediately(self):
        with patch.object(d, 'mcp_snapshot', new_callable=AsyncMock) as snapshot, patch.object(d.time, 'sleep'):
            snapshot.side_effect = [ConnectionRefusedError(), {'ready': True}]
            self.assertEqual({'ready': True}, d.check_mcp([]))
            self.assertEqual(2, snapshot.call_count)
            snapshot.reset_mock(); snapshot.side_effect = d.DeploymentError('stale MCP edition')
            with self.assertRaises(d.DeploymentError): d.check_mcp([])
            self.assertEqual(1, snapshot.call_count)


if __name__ == '__main__':
    unittest.main()
