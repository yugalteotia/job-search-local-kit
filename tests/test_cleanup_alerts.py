import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import workflow as w
import openpyxl
import terminal_cleanup as tc
import urgent_alerts as alerts

class CleanupAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.old=(w.ROOT,w.STATE,alerts.STATE)
        w.ROOT=self.root;w.STATE=self.root/'.workflow';alerts.STATE=w.STATE/'alerts'
        wb=openpyxl.Workbook();ws=wb.active;ws.title='Tracker'
        ws.append(['Company','Role','Job ID','Direct link','Status','Verified date','Submission date','Confirmation evidence','Responses','Next step','Location'])
        wb.save(self.root/'tracker.xlsx')
    def tearDown(self):
        w.ROOT,w.STATE,alerts.STATE=self.old;self.temp.cleanup()
    def terminal_fixture(self):
        row={'Company':'Acme','Role':'Java Developer','Job ID':'abc123','Direct link':'https://acme.example/jobs/abc123',
             'Status':'Rejected','Submission date':'2026-09-28','Responses':'Updated Sep 28, 2026; no reason shown',
             'Location':'Pune','Next step':'Do not retry'}
        w.upsert(row,'test')
        folder=w.job_folder('acme-abc123');folder.mkdir(parents=True)
        w.write_json(folder/'listing.json',{'company':'Acme','role':'Java Developer','job_id':'abc123','url':row['Direct link']})
        (folder/'Tailored.pdf').write_bytes(b'old pdf')
        (folder/'confirmation.json').write_text(json.dumps({'captured_at':w.now()}))
        cap=self.root/'captures/abc123.json';cap.parent.mkdir();cap.write_text('{"job_id":"abc123"}')
        return folder
    def test_preview_cleanup_and_30_day_proof(self):
        folder=self.terminal_fixture()
        summary=tc.preview(w)
        self.assertEqual(summary['terminal_rows'],1)
        self.assertEqual(summary['delete_files'],3)
        self.assertEqual(summary['retained_proof_files'],1)
        applied=tc.apply(w)
        self.assertEqual(len(applied['deleted_files']),3)
        row=w.tracker_rows()[3][0]
        self.assertEqual(row['Status date'],'2026-09-28')
        self.assertEqual(row['Known reason'],'reason unknown')
        self.assertIsNone(row['Location'])
        self.assertTrue((folder/'confirmation.json').exists())
        self.assertFalse((folder/'Tailored.pdf').exists())
        self.assertEqual(applied['views']['quick_rows'],0)
        due=dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=31)
        self.assertEqual(len(tc.prune_retained(w,at=due)['pruned']),1)
        self.assertFalse((folder/'confirmation.json').exists())
        self.assertEqual(w.tracker_rows()[3][0]['Job ID'],'abc123')
        with self.assertRaises(ValueError):w.ingest(self._new_packet())
    def _new_packet(self):
        p=self.root/'new.json';w.write_json(p,{'company':'Acme','role':'Java Developer','job_id':'abc123',
          'url':'https://acme.example/jobs/abc123','checked_at':w.now(),'jd':'Java backend',
          'slug':'acme-abc123','live':True,'original_verified':True,'india':True,'development_role':True,
          'requirements':[{'name':'Java','required':True,'basis':'project','evidence':'project'}]})
        return p
    def test_changed_file_refuses_cleanup(self):
        folder=self.terminal_fixture();tc.preview(w)
        (folder/'Tailored.pdf').write_bytes(b'changed')
        with self.assertRaises(ValueError):tc.apply(w)
        self.assertEqual(w.tracker_rows()[3][0]['Location'],'Pune')
    def test_stale_is_not_expired(self):
        self.assertFalse(tc.is_terminal('Excluded — stale'))
        self.assertTrue(tc.is_terminal('Excluded — closed'))
        self.assertTrue(tc.is_terminal('Expired'))
    def alert_fixture(self):
        for slug,job_id in [('one','1'),('two','2')]:
            w.upsert({'Company':'Acme','Role':'Java '+slug,'Job ID':job_id,'Status':'Shortlisted'},'test')
            folder=w.job_folder(slug);folder.mkdir(parents=True)
            w.write_json(folder/'listing.json',{'company':'Acme','role':'Java '+slug,'job_id':job_id,
                       'url':'https://acme.example/jobs/'+job_id+'?session=private','fit':'High','original_verified':True})
        packet=self.root/'blocker.json'
        w.write_json(packet,{'kind':'captcha','blocker_id':'a'*64})
        return packet
    def test_only_one_job_blocked_rate_limited_and_acknowledged(self):
        packet=self.alert_fixture()
        with patch.object(alerts,'desktop',return_value={'notification_sent':True,'sound_played_command':True,'notification_id':42}) as sound,patch.object(alerts,'phone_config',return_value=None),patch.object(alerts,'_close_desktop',return_value=True):
            self.assertTrue(alerts.mark(w,'one',packet)['urgent'])
            alerts.mark(w,'one',packet)
            self.assertEqual(sound.call_count,1)
            self.assertTrue(alerts.acknowledge(w,'one')['acknowledged'])
            alerts.mark(w,'one',packet)
            self.assertEqual(sound.call_count,1)
        rows=w.tracker_rows()[3]
        self.assertEqual([r['Status'] for r in rows],['Needs Answer','Shortlisted'])
        import tracker_views
        tracker_views.views(w)
        self.assertEqual([r['Status'] for r in w.read_json(w.ROOT/'views/quick.json')],['Needs Answer','Shortlisted'])
    def test_ordinary_question_batches_and_payload_rejects_contents(self):
        packet=self.alert_fixture();w.write_json(packet,{'kind':'unknown_required_answer','blocker_id':'b'*64})
        with patch.object(alerts,'desktop') as desktop,patch.object(alerts,'phone_config',return_value=None):
            self.assertFalse(alerts.mark(w,'one',packet)['urgent'])
            desktop.assert_not_called()
        w.write_json(packet,{'kind':'captcha','blocker_id':'c'*64,'answer':'private'})
        with self.assertRaises(ValueError):alerts.mark(w,'two',packet)
        self.assertEqual(w.tracker_rows()[3][1]['Status'],'Shortlisted')
    def test_phone_message_has_only_generic_action_and_safe_link(self):
        packet=self.alert_fixture()
        config={'enabled':True,'user':'U'*30,'token':'T'*30}
        with patch.object(alerts,'desktop',return_value={'notification_sent':True,'sound_played_command':True,'notification_id':1}),patch.object(alerts,'phone_config',return_value=config),patch.object(alerts,'api_post',return_value={'ok':True,'receipt':'R'*30}) as sender:
            self.assertTrue(alerts.mark(w,'one',packet)['phone_queued'])
        payload=sender.call_args.args[1]
        self.assertEqual(set(payload),{'user','title','message','url','url_title','priority','retry','expire'})
        self.assertEqual(payload['message'],'Complete CAPTCHA in browser')
        self.assertEqual(payload['url'],'https://acme.example/jobs/1')
        self.assertNotIn('private',json.dumps(payload))
        self.assertEqual(payload['priority'],2)
    def test_sqlite_blocker_sync_alerts_medium_naukri_jobs(self):
        class Result:
            def fetchall(self):
                return [{'id':314,'slug':'naukri-medium','canonical_url':'https://www.naukri.com/job-listings-java-backend-314',
                         'status_date':'2026-09-29T10:00:00+00:00','requisition_id':'314','state':'NeedsAnswer',
                         'attempt_id':None,'attempt_state':None}]
        class DB:
            def __init__(self):self.query=''
            def execute(self,query):self.query=query;return Result()
        db=DB();cfg={'alerts':{'desktop_enabled':True,'phone_enabled':False,'repeat_cooldown_minutes':360}}
        with patch.object(alerts,'desktop',return_value={'notification_sent':True,'notification_id':7}),\
             patch.object(alerts,'phone_config',return_value=None):
            result=alerts.sync_sqlite_blockers(db,cfg)
        self.assertIn("j.fit IN ('High','Medium')",db.query)
        self.assertEqual(result[0]['job_id'],314)
        self.assertTrue(result[0]['desktop_sent'])

    def test_uncertain_submission_alert_says_do_not_retry(self):
        class Result:
            def fetchall(self):
                return [{'id':315,'slug':'naukri-uncertain','canonical_url':'https://www.naukri.com/job-listings-java-backend-315',
                         'status_date':'2026-09-29T10:00:00+00:00','requisition_id':'315','state':'SubmissionUnconfirmed',
                         'attempt_id':'attempt-315','attempt_state':'uncertain'}]
        class DB:
            def execute(self,query):self.query=query;return Result()
        db=DB();cfg={'alerts':{'desktop_enabled':True,'phone_enabled':False,'repeat_cooldown_minutes':360}}
        with patch.object(alerts,'desktop',return_value={'notification_sent':True,'notification_id':8}) as desktop, patch.object(alerts,'phone_config',return_value=None):
            result=alerts.sync_sqlite_blockers(db,cfg)
        self.assertIn("j.state='SubmissionUnconfirmed'",db.query)
        self.assertTrue(result[0]['desktop_sent'])
        self.assertIn('do not retry',desktop.call_args.args[1])

    def test_new_blocked_job_alert_is_independent_of_uncertain_cooldown(self):
        from unittest.mock import MagicMock
        old={'id':220,'slug':'old-uncertain','canonical_url':'https://www.naukri.com/job-listings-java-220',
             'status_date':'2026-10-13','state':'SubmissionUnconfirmed','attempt_id':'old','attempt_state':'uncertain'}
        new={'id':346,'slug':'new-blocked','canonical_url':'https://www.naukri.com/job-listings-java-346',
             'status_date':'2026-10-13','state':'ResumeReady','attempt_id':None,'attempt_state':None}
        folder=self.root/'jobs/new-blocked';folder.mkdir(parents=True)
        (folder/'naukri-native-outcome.json').write_text(json.dumps({'status':'Blocked','stage':'applied_history_check',
            'error_type':'RuntimeError','error_message':'Unresolved company/title matches; verify exact requisition IDs'}))
        alerts.STATE.mkdir(parents=True)
        (alerts.STATE/'state.json').write_text(json.dumps({'sqlite-old-uncertain':{
            'blocker_id':'220:2026-10-13:old','kind':'uncertain_submission','acknowledged':False,
            'last_desktop_sent':alerts.utcnow().isoformat()}}))
        db=MagicMock();db.execute.return_value.fetchall.return_value=[old,new]
        cfg={'alerts':{'desktop_enabled':True,'phone_enabled':False,'repeat_cooldown_minutes':360}}
        with patch.object(alerts,'ROOT',self.root),patch.object(alerts,'desktop',return_value={'notification_sent':True,'notification_id':9}) as desktop:
            result=alerts.sync_sqlite_blockers(db,cfg)
        self.assertEqual(result[0]['status'],'cooldown');self.assertTrue(result[1]['desktop_sent'])
        desktop.assert_called_once();self.assertIn('Unresolved company/title matches',desktop.call_args.args[1])
    def test_resolve_requires_matching_blocker_and_evidence(self):
        packet=self.alert_fixture()
        with patch.object(alerts,'desktop',return_value={'notification_sent':True,'sound_played_command':True,'notification_id':42}),patch.object(alerts,'phone_config',return_value=None),patch.object(alerts,'_close_desktop',return_value=True):
            alerts.mark(w,'one',packet)
            review=self.root/'resolution.json'
            w.write_json(review,{'resolved':True,'blocker_id':'b'*64,'next_status':'Prepared','evidence':'proof.txt'})
            (self.root/'proof.txt').write_text('CAPTCHA completed, current form verified')
            with self.assertRaises(ValueError):alerts.resolve(w,'one',review)
            w.write_json(review,{'resolved':True,'blocker_id':'a'*64,'next_status':'Prepared','evidence':'proof.txt'})
            self.assertTrue(alerts.resolve(w,'one',review)['resolved'])
            self.assertEqual(w.tracker_rows()[3][0]['Status'],'Prepared')
            self.assertEqual(alerts.status(w)['open_jobs'],[])
    def test_expiring_role_requires_real_deadline_evidence(self):
        packet=self.alert_fixture()
        w.write_json(packet,{'kind':'expiring_high_fit','blocker_id':'d'*64})
        with self.assertRaises(ValueError):alerts.mark(w,'one',packet)
        proof=self.root/'jobs/one/JD.md';proof.write_text('Apply by tomorrow')
        deadline=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=12)).isoformat()
        w.write_json(packet,{'kind':'expiring_high_fit','blocker_id':'d'*64,
                    'deadline':deadline,'deadline_evidence':'jobs/one/JD.md'})
        with patch.object(alerts,'desktop',return_value={'notification_sent':True,'sound_played_command':True,'notification_id':1}),patch.object(alerts,'phone_config',return_value=None):
            self.assertTrue(alerts.mark(w,'one',packet)['urgent'])
        w.write_json(packet,{'kind':'unknown_required_answer','blocker_id':'e'*64,
                    'deadline':(dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=48)).isoformat(),
                    'deadline_evidence':'jobs/one/JD.md'})
        with patch.object(alerts,'desktop'),patch.object(alerts,'phone_config',return_value=None),patch.object(alerts,'_close_desktop',return_value=True):
            self.assertFalse(alerts.mark(w,'one',packet)['urgent'])
    def test_confirm_phone_needs_acknowledged_receipt(self):
        directory=self.root/'private';directory.mkdir(mode=0o700)
        phone=directory/'pushover.json';phone.write_text(json.dumps({'token':'T'*30,'user':'U'*30,'enabled':False}));phone.chmod(0o600)
        w.write_json(alerts.STATE/'phone-test.json',{'receipt':'R'*30,'confirmed':False})
        with patch.object(alerts,'PRIVATE',directory),patch.object(alerts,'PHONE',phone):
            with patch.object(alerts,'receipt_status',return_value={'ok':True,'acknowledged':False}):
                self.assertFalse(alerts.confirm_phone(w)['enabled'])
            with patch.object(alerts,'receipt_status',return_value={'ok':True,'acknowledged':True}):
                self.assertTrue(alerts.confirm_phone(w)['enabled'])
            self.assertTrue(alerts.status(w)['phone_enabled'])

if __name__=='__main__':unittest.main()
