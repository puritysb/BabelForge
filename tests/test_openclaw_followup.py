"""Scheduled follow-up tests use temporary evidence and mocked Gateway/LINE only."""
from contextlib import contextmanager
import copy
from datetime import datetime
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import openclaw_followup as followup
import install_openclaw_followup as installer
import openclaw_monitor as monitor
from state_store import file_lock
import test_openclaw_line_drill as fixtures


class FollowupTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        with fixtures.DrillTests().fixture() as f:
            plan = installer.prepare(f['settings_path'], 'existing', '2099-01-01')
            due = plan['scheduled_ms']; now = plan['followup_ms']/1000+1
            def iso(delta): return datetime.fromtimestamp((due+delta)/1000).astimezone().isoformat()
            f['report']['started_at'] = iso(1000); f['report']['finished_at'] = iso(50000)
            identifier = 'a'*32
            report_path = f['data']/'deployments'/f'openclaw-{identifier}.json'
            monitor.save(report_path, f['report'])
            record = {'report': str(report_path), 'log': str(f['runtime']/f'{identifier}.log'),
                      'completed': True, 'notification': 'not-requested',
                      'started_at': iso(500), 'finished_at': iso(51000)}
            record_path = f['runtime']/f'{identifier}.json'; monitor.save(record_path, record)
            monitor.save(f['runtime']/'incident.json', {'fingerprint': None})
            entry = {'jobId': 'existing', 'runId': 'scheduled:existing:one', 'action': 'finished',
                     'runAtMs': due, 'ts': due+52000, 'status': 'ok', 'completionStatus': 'succeeded',
                     'diagnostics': {'summary': 'NO_REPLY'}, 'deliveryStatus': 'not-requested'}
            directory = f['data']/'openclaw-monitor/followups/test'
            plan_path = directory/'plan.json'; monitor.save(plan_path, plan)
            with patch.object(followup, 'history', return_value=[entry]) as history, \
                    patch.object(followup.time, 'time', return_value=now):
                yield {**f, **locals()}

    def execute(self, f):
        return followup.run(f['plan_path'], execute=True)

    def failed_run(self, f, *, outcome='sent', delivery='sent', fingerprint=None):
        f['entry'].update(status='error', completionStatus='failed')
        f['record'].update(completed=False, error='ValueError: EPUB invalid', error_stage='local_validation',
                           notification=outcome)
        fp = monitor.incident(f['record']['error'], 'local_validation', f['settings']['expected'])
        f['record']['incident_fingerprint'] = fp
        monitor.save(f['record_path'], f['record'])
        monitor.save(f['runtime']/'incident.json', {'fingerprint': fingerprint or fp, 'delivery': delivery})

    def test_healthy_scheduled_run_joins_evidence_and_remains_silent(self):
        with self.fixture() as f:
            before = {p.name:p.read_bytes() for p in f['runtime'].glob('*.json')}
            result = self.execute(f)
            self.assertTrue(result['completed']); self.assertEqual('not-requested', result['notification'])
            self.assertEqual(1, result['editions']); self.assertEqual(3, result['approved_pairs'])
            self.assertEqual(f['entry']['runId'], result['run_id']); f['send'].assert_not_called()
            self.assertEqual(before, {p.name:p.read_bytes() for p in f['runtime'].glob('*.json')})
            self.assertEqual(0o600, (f['directory']/'result.json').stat().st_mode & 0o777)

    def test_preview_and_early_manual_execution_do_not_emit_missing_run_alerts(self):
        with self.fixture() as f:
            self.assertFalse(followup.run(f['plan_path'])['message_sent'])
            with patch.object(followup.time, 'time', return_value=f['due']/1000):
                self.assertTrue(self.execute(f)['not_due'])
            f['history'].assert_not_called(); f['send'].assert_not_called()
            self.assertFalse((f['directory']/'result.json').exists())

    def test_missing_manual_wrong_job_and_duplicate_runs_never_certify_success(self):
        for problem in ('missing', 'manual', 'wrong_job', 'duplicate', 'old', 'late', 'unfinished', 'boolean_time'):
            with self.subTest(problem=problem), self.fixture() as f:
                e=f['entry']
                if problem=='missing': f['history'].return_value=[]
                elif problem=='manual': e['runId']='manual:existing:one'
                elif problem=='wrong_job': e['jobId']='other'
                elif problem=='duplicate': f['history'].return_value=[e,dict(e,runId='another')]
                elif problem=='old': e['runAtMs']-=86400000
                elif problem=='late': e['runAtMs']+=301000
                elif problem=='unfinished': e['ts']=f['plan']['followup_ms']+1
                else: e['runAtMs']=True
                self.assertFalse(self.execute(f)['completed']); f['send'].assert_called_once()

    def test_successful_manual_run_outside_target_window_cannot_replace_missing_schedule(self):
        with self.fixture() as f:
            f['entry']['runId']='manual:existing:one';f['entry']['runAtMs']=f['due']-60000
            self.assertIn('run_missing',self.execute(f)['issues']); f['send'].assert_called_once()

    def test_latest_only_stale_report_or_wrong_paths_are_not_linked(self):
        for problem in ('latest_only', 'stale', 'drill_path', 'symlink', 'missing_report', 'bad_timezone'):
            with self.subTest(problem=problem), self.fixture() as f:
                if problem=='latest_only':
                    f['record_path'].unlink();monitor.save(f['runtime']/'latest.json',f['record'])
                elif problem=='stale':
                    f['report']['started_at']='2020-01-01T00:00:00+00:00';monitor.save(f['report_path'],f['report'])
                elif problem=='drill_path':
                    f['record']['report']=str(f['directory']/'fake.json');monitor.save(f['record_path'],f['record'])
                elif problem=='symlink':
                    f['report_path'].unlink();f['report_path'].symlink_to(f['manifest'])
                elif problem=='missing_report': f['report_path'].unlink()
                else:
                    f['record']['started_at']='2099-01-01T09:00:00';monitor.save(f['record_path'],f['record'])
                result=self.execute(f);self.assertFalse(result['completed']); f['send'].assert_called_once()

    def test_overlapping_report_records_are_rejected(self):
        with self.fixture() as f:
            other=dict(f['record'],report=str(f['report_path'].with_name('openclaw-'+'b'*32+'.json')))
            monitor.save(Path(other['report']),f['report']);monitor.save(f['runtime']/('b'*32+'.json'),other)
            self.assertIn('report_unlinked',self.execute(f)['issues'])

    def test_legacy_success_records_can_link_only_via_their_fresh_report_times(self):
        with self.fixture() as f:
            f['record'].pop('started_at'); f['record'].pop('finished_at');monitor.save(f['record_path'],f['record'])
            self.assertTrue(self.execute(f)['completed']); f['send'].assert_not_called()

    def test_incomplete_approval_epub_inventory_or_version_results_fail_closed(self):
        for problem in ('epub', 'pairs', 'inventory', 'version', 'incomplete', 'identity'):
            with self.subTest(problem=problem), self.fixture() as f:
                r=f['report']
                if problem=='epub': r['editions'][0]['public']['stable_validation']['epubcheck']='not_available'
                elif problem=='pairs': r['approved_pairs']=True
                elif problem=='inventory': r['inventory']['duplicates']=1
                elif problem=='version': r['semantic_version']='obsolete'
                elif problem=='incomplete': r['completed']=False
                else: r['editions'][0]['sha256']='different'
                monitor.save(f['report_path'],r)
                self.assertIn('verification_failed',self.execute(f)['issues']);f['send'].assert_called_once()

    def test_normal_delivery_or_non_silent_summary_is_an_anomaly(self):
        for field in ('record', 'delivery', 'summary'):
            with self.subTest(field=field),self.fixture() as f:
                if field=='record': f['record']['notification']='sent';monitor.save(f['record_path'],f['record'])
                elif field=='delivery': f['entry']['deliveryStatus']='delivered'
                else: f['entry']['diagnostics']['summary']='Everything fine!'
                self.assertIn('normal_not_silent',self.execute(f)['issues']);f['send'].assert_called_once()

    def test_proven_daily_alerts_and_uncertain_receipts_are_not_replayed(self):
        for outcome,delivery,expected in [('sent','sent','already-notified'),('suppressed','sent','already-notified'),
                                           ('unknown','unknown','ambiguous-receipt-no-replay'),
                                           ('unknown','pending','ambiguous-receipt-no-replay')]:
            with self.subTest(outcome=outcome,delivery=delivery),self.fixture() as f:
                self.failed_run(f,outcome=outcome,delivery=delivery)
                result=self.execute(f);self.assertFalse(result['completed']);self.assertEqual(expected,result['notification'])
                f['send'].assert_not_called()

    def test_unrelated_or_inconsistent_incident_does_not_suppress_new_alert(self):
        with self.fixture() as f:
            self.failed_run(f,fingerprint='different')
            self.assertEqual('sent',self.execute(f)['notification']);f['send'].assert_called_once()

    def test_other_schedule_anomaly_is_not_hidden_by_already_notified_daily_failure(self):
        with self.fixture() as f:
            self.failed_run(f); f['job']['schedule']['expr']='0 10 * * *'
            result=self.execute(f);self.assertIn('schedule_changed',result['issues']);f['send'].assert_called_once()

    def test_failure_without_checker_report_can_link_by_monitor_timestamps(self):
        with self.fixture() as f:
            self.failed_run(f);f['report_path'].unlink()
            self.assertEqual('already-notified',self.execute(f)['notification']);f['send'].assert_not_called()

    def test_reexecution_or_changed_error_never_sends_twice(self):
        with self.fixture() as f:
            f['history'].return_value=[]
            self.assertEqual('sent',self.execute(f)['notification'])
            f['history'].side_effect=RuntimeError('private details')
            self.assertEqual('suppressed',self.execute(f)['notification']);f['send'].assert_called_once()
            self.assertNotIn('private details',(f['directory']/'result.json').read_text())

    def test_followup_timeout_or_unknown_receipt_never_automatically_replays(self):
        for problem in ('timeout','unknown'):
            with self.subTest(problem=problem),self.fixture() as f:
                f['history'].return_value=[]
                if problem=='timeout':f['send'].side_effect=subprocess.TimeoutExpired('send',45)
                else:f['send'].return_value.stdout='{}'
                self.assertEqual('unknown',self.execute(f)['notification'])
                self.assertEqual('unknown-no-replay',self.execute(f)['notification']);f['send'].assert_called_once()

    def test_settings_drift_uses_pinned_approved_route_and_never_rebases_baseline(self):
        with self.fixture() as f:
            changed=dict(f['settings'],line_target='unapproved-owner');monitor.save(f['settings_path'],changed)
            result=self.execute(f);self.assertIn('settings_changed',result['issues'])
            argv=f['send'].call_args.args[0];self.assertEqual('private-owner',argv[argv.index('--target')+1])
            self.assertNotIn('private-owner',(f['directory']/'result.json').read_text())

    def test_manifest_change_during_history_read_is_detected(self):
        with self.fixture() as f:
            def changed(*args):
                f['manifest'].write_text('{"changed":true}')
                return [f['entry']]
            f['history'].side_effect=changed
            self.assertIn('settings_changed',self.execute(f)['issues']);f['send'].assert_called_once()

    def test_corrupt_followup_receipt_is_not_automatically_replayed(self):
        with self.fixture() as f:
            (f['directory']/'delivery.json').write_text('broken')
            f['history'].return_value=[]
            self.assertEqual('unknown-no-replay',self.execute(f)['notification']);f['send'].assert_not_called()

    def test_pending_attempt_survives_interruption_without_replay(self):
        with self.fixture() as f:
            f['history'].return_value=[];f['send'].side_effect=KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):self.execute(f)
            self.assertEqual({'status':'pending'},json.loads((f['directory']/'delivery.json').read_text()))
            f['send'].side_effect=None
            self.assertEqual('unknown-no-replay',self.execute(f)['notification']);f['send'].assert_called_once()

    def test_production_and_followup_directories_cannot_overlap(self):
        with self.fixture() as f:
            plan_path=f['runtime']/'plan.json';monitor.save(plan_path,f['plan'])
            with self.assertRaisesRegex(ValueError,'overlap'):followup.run(plan_path,execute=True)
            f['history'].assert_not_called();f['send'].assert_not_called()

    def test_inconsistent_ledger_fingerprint_cannot_hide_an_alert(self):
        with self.fixture() as f:
            self.failed_run(f);f['record']['incident_fingerprint']='unrelated'
            monitor.save(f['record_path'],f['record'])
            self.assertEqual('sent',self.execute(f)['notification']);f['send'].assert_called_once()

    def test_changed_job_policy_prevents_installer_success(self):
        with self.fixture() as f,patch.object(installer,'job_policy',return_value={'changed':True}):
            # This path is rejected before any scheduler mutation by the preflight below.
            with self.assertRaises(ValueError):installer.install(f['plan'])
            f['send'].assert_not_called()

    def test_history_failure_does_not_skip_independent_schedule_audits(self):
        with self.fixture() as f,patch.object(followup,'job_policy',return_value=f['plan']['daily_policy']) as policy:
            f['history'].side_effect=RuntimeError('Gateway unavailable')
            self.assertIn('history_unavailable',self.execute(f)['issues']);self.assertEqual(2,policy.call_count)

    def test_late_schedule_change_is_detected(self):
        with self.fixture() as f,patch.object(followup,'job_policy',side_effect=[f['plan']['daily_policy'],ValueError('drift')]):
            result=self.execute(f);self.assertFalse(result['schedule_unchanged']);f['send'].assert_called_once()

    def test_concurrent_followup_cannot_duplicate_notification(self):
        with self.fixture() as f,file_lock(str(f['directory']/'followup.lock')):
            with self.assertRaises(BlockingIOError):self.execute(f)
            f['history'].assert_not_called();f['send'].assert_not_called()

    def test_history_pagination_is_complete_and_fail_closed(self):
        with patch.object(followup,'cli',side_effect=[{'entries':[{'runId':'a'}],'hasMore':True,'nextOffset':100},
                                                     {'entries':[{'runId':'b'}],'hasMore':False}]) as cli:
            self.assertEqual(2,len(followup.history('/cli','job')))
            self.assertIn('100',cli.call_args.args)
        for page in ({'entries':[],'hasMore':True,'nextOffset':0},{'entries':[]},
                     {'entries':[{'runId':'a'},{'runId':'a'}],'hasMore':False}):
            with self.subTest(page=page),patch.object(followup,'cli',return_value=page):
                with self.assertRaises(ValueError):followup.history('/cli','job')

    def test_history_budget_leaves_time_to_audit_schedule_and_alert(self):
        with patch.object(followup.time,'monotonic',side_effect=[0,0,241]), \
                patch.object(followup,'cli',return_value={'entries':[],'hasMore':True,'nextOffset':100}) as cli:
            with self.assertRaisesRegex(ValueError,'time budget'):followup.history('/cli','job')
            cli.assert_called_once()

    def test_installer_validates_future_date_grace_and_baseline(self):
        with self.fixture() as f:
            for day,delay in [('2000-01-01',30),('2099-01-01',29),('2099-01-01',True)]:
                with self.subTest(day=day,delay=delay),self.assertRaises(ValueError):
                    installer.prepare(f['settings_path'],'existing',day,delay_minutes=delay)
            self.assertEqual(1,len(f['plan']['settings']['expected']['editions']))

    def test_installation_rejects_config_drift_after_preview_before_mutating_scheduler(self):
        with self.fixture() as f,patch.object(installer,'cli') as cli:
            changed=dict(f['settings'],line_target='another-owner');monitor.save(f['settings_path'],changed)
            with self.assertRaisesRegex(ValueError,'Changed settings'):installer.install(f['plan'])
            cli.assert_not_called();f['send'].assert_not_called()

    def test_snapshot_must_match_pinned_config_as_well_as_its_digest(self):
        with self.fixture() as f:
            f['plan']['settings']['line_target']='corrupt-snapshot'
            with self.assertRaisesRegex(ValueError,'Changed settings'):followup.baseline_unchanged(f['plan'])

    def test_installer_is_idempotent_and_never_edits_daily_job_or_receipts(self):
        with self.fixture() as f:
            jobs=[]; calls=[]
            def cli(binary,action,*args):
                calls.append((action,args))
                if action=='list':return {'jobs':copy.deepcopy(jobs)}
                if action=='add':
                    jobs.append({'id':'follow','name':args[args.index('--name')+1],
                                 'declarationKey':args[args.index('--declaration-key')+1]})
                    self.assertIn('--disabled',args);return jobs[-1]
                if action=='edit':
                    self.assertEqual('follow',args[0]);self.assertIn('--no-failure-alert',args)
                    jobs[0].update(enabled=True,deleteAfterRun=False,delivery={'mode':'none'},failureAlert=False,
                        schedule={'kind':'at','at':args[args.index('--at')+1]},
                        payload={'kind':'command','argv':json.loads(args[args.index('--command-argv')+1]),
                                 'cwd':str(f['root']),'env':{'JAVA_HOME':'/jdk','PATH':f['settings']['execution_path']},
                                 'timeoutSeconds':600,'noOutputTimeoutSeconds':600})
                    return jobs[0]
                self.assertEqual('get',action);return copy.deepcopy(jobs[0])
            with patch.object(installer,'cli',side_effect=cli):
                receipt=installer.install(f['plan']);directory=Path(receipt['plan']).parent
                monitor.save(directory/'delivery.json',{'status':'unknown'})
                self.assertEqual(receipt,installer.install(f['plan']))
                self.assertEqual({'status':'unknown'},json.loads((directory/'delivery.json').read_text()))
                self.assertEqual(1,len([x for x in calls if x[0]=='add']))
                changed=copy.deepcopy(f['plan']);changed['start_grace_seconds']=301
                with self.assertRaisesRegex(ValueError,'plan changed'):installer.install(changed)
            self.assertEqual(0o600,Path(receipt['plan']).stat().st_mode & 0o777)
            f['send'].assert_not_called()

    def test_installer_rejects_foreign_duplicate_and_unconfirmed_legacy_jobs(self):
        with self.fixture() as f:
            name='babelforge-followup-'+f['plan']['identity'];legacy='babelforge-mcp-opds-followup-2099-01-01'
            cases=[([{'id':'one','name':name,'declarationKey':'foreign'}],None),
                   ([{'id':'one','name':legacy,'declarationKey':legacy+'-v1'}],None),
                   ([{'id':'one','name':name},{'id':'two','name':name}],None),([], 'unrelated')]
            for jobs,identifier in cases:
                with self.subTest(jobs=jobs),patch.object(installer,'cli',return_value={'jobs':jobs}) as cli:
                    with self.assertRaises(ValueError):installer.install(f['plan'],existing_job_id=identifier)
                    self.assertEqual(1,cli.call_count)

    def test_installer_disables_unverified_followup_without_touching_daily_job(self):
        with self.fixture() as f:
            def cli(binary,action,*args):
                if action=='list':return {'jobs':[]}
                if action=='add':return {'id':'follow'}
                self.assertEqual('follow',args[0])
                return {'enabled':False}
            with patch.object(installer,'cli',side_effect=cli) as calls:
                with self.assertRaises(ValueError):installer.install(f['plan'])
                final=calls.call_args.args
                self.assertEqual(('/test/openclaw','edit','follow'),final[:3])
                self.assertIn('--disable',final);self.assertIn('--no-failure-alert',final)
            f['send'].assert_not_called()

    def test_corrupt_plan_bounds_are_rejected_before_gateway_or_notification(self):
        with self.fixture() as f:
            f['plan']['scheduled_ms']=True;monitor.save(f['plan_path'],f['plan'])
            with self.assertRaisesRegex(ValueError,'bounds'):self.execute(f)
            f['history'].assert_not_called();f['send'].assert_not_called()


if __name__=='__main__':unittest.main()
