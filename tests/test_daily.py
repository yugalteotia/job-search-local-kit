import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import workflow as w
import daily_runner as d
import openpyxl

class DailySafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.old=(w.ROOT,w.STATE,d.ROOT,d.STATE)
        w.ROOT=d.ROOT=self.root;w.STATE=d.STATE=self.root/'.workflow'
        wb=openpyxl.Workbook();ws=wb.active;ws.title='Tracker'
        ws.append(['Company','Role','Job ID','Direct link','Status','Confirmation evidence','Responses','Next step','Submission date'])
        wb.save(self.root/'tracker.xlsx')
    def tearDown(self):
        w.ROOT,w.STATE,d.ROOT,d.STATE=self.old;self.temp.cleanup()
    def test_schedule_requires_actual_private_probe(self):
        for result in [{},{'passed':True},{'passed':False,'private_token_transport':True}]:
            w.write_json(w.STATE/'browser-probe-private.json',result)
            with self.assertRaises(ValueError): d.gate(w)
    def test_timer_has_exact_zone_and_no_catchup(self):
        body=d.unit_files()['job-search-daily.timer']
        self.assertIn('08,12,16,20:30:00 Asia/Kolkata',body);self.assertIn('Persistent=false',body)
    def test_all_four_windows_and_missed_runs(self):
        for hour in d.SCHEDULE_HOURS:
            for minute,allowed in [(29,False),(30,True),(44,True),(45,False)]:
                self.assertEqual(d.schedule_window(dt.datetime(2026,9,28,hour,minute,tzinfo=d.IST)),allowed)
        self.assertFalse(d.schedule_window(dt.datetime(2026,9,28,9,30,tzinfo=d.IST)))
    def test_next_slots_roll_over_after_evening(self):
        slots=d.next_slots(dt.datetime(2026,9,28,21,0,tzinfo=d.IST))
        self.assertEqual([s.hour for s in slots],[8,12,16,20])
        self.assertTrue(all(s.day==29 for s in slots))
    def test_title_and_keywords_cannot_make_high_and_old_posting_can_still_fit(self):
        packet={'original_verified':True,'live':True,'india':True,'development_role':True,'posted_date':'2026-08-01','requirements':[{'name':'Java backend','required':True,'basis':'work','master_bullet':[0,0]}]}
        self.assertNotEqual(w.score(packet)[0],'High')
        packet['fit_checks']={k:{'verified':True,'evidence':'Original JD and confirmed profile comparison'} for k in ['java_spring_backend','experience_band','india_location']}
        self.assertEqual(w.score(packet)[0],'High')
        packet['requirements'].append({'name':'Essential production Kafka','required':True,'basis':'gap','evidence':'Not demonstrated'})
        self.assertNotEqual(w.score(packet)[0],'High')
    def test_response_metadata_does_not_regress_applied(self):
        w.upsert({'Company':'Acme','Role':'Java','Job ID':'a','Status':'Applied','Confirmation evidence':'old-proof'},'test')
        w.upsert({'Company':'Acme','Job ID':'a','Responses':'Recruiter replied'},'response')
        self.assertEqual(w.tracker_rows()[3][0]['Status'],'Applied')
    def test_uncertain_to_applied_requires_daily_authorization_and_proof(self):
        row={'Company':'Acme','Role':'Java','Job ID':'a'}
        w.upsert({**row,'Status':'Submission unconfirmed'},'test')
        proof=self.root/'proof.txt';proof.write_text('Application received')
        with self.assertRaises(ValueError): w.upsert({**row,'Status':'Applied','Confirmation evidence':'proof.txt'},'test',authorized_transition=True)
        w.write_json(w.STATE/'daily-authorization.json',{'authorized':True})
        w.upsert({**row,'Status':'Applied','Confirmation evidence':'proof.txt'},'test',authorized_transition=True)
        with self.assertRaises(ValueError): w.upsert({**row,'Status':'Shortlisted'},'rediscovery')
    def test_optum_never_retries_under_daily_authorization(self):
        row={'Company':'Optum','Role':'Java','Job ID':'2388999'}
        w.upsert({**row,'Status':'Rejected'},'test')
        w.write_json(w.STATE/'daily-authorization.json',{'authorized':True})
        with self.assertRaises(ValueError): w.upsert({**row,'Status':'Submission unconfirmed'},'test',authorized_transition=True)
    def test_begin_submit_blocks_missing_review_and_existing_attempt(self):
        w.write_json(w.STATE/'control.json',{'submission_enabled':True})
        folder=w.job_folder('acme');folder.mkdir(parents=True)
        w.write_json(folder/'listing.json',{'company':'Acme','role':'Java','job_id':'a','fit':'High','original_verified':True,'live':True,'checked_at':w.now()})
        w.upsert({'Company':'Acme','Role':'Java','Job ID':'a','Status':'Prepared'},'test')
        review=self.root/'review.json';w.write_json(review,{})
        with patch.object(d,'gate'),patch.object(w,'verify_visual_review'),patch.object(w,'score',return_value=('High',100)):
            with self.assertRaises(ValueError): w.begin_submission('acme',review)
            flags={k:True for k in ['required_answers_confirmed','live_values_verified','resume_attachment_verified','history_deduplicated','certification_approved']}
            proof=self.root/'review.txt';proof.write_text('Safe verification evidence')
            w.write_json(review,{**flags,'verified_at':w.now(),'form_evidence':'review.txt','history_evidence':'review.txt','certification_evidence':'review.txt'})
            self.assertTrue(w.begin_submission('acme',review)['authorized_to_click_once'])
            self.assertEqual(w.tracker_rows()[3][0]['Status'],'Submission unconfirmed')
            with self.assertRaises(ValueError): w.begin_submission('acme',review)
            self.assertEqual(len(list((w.STATE/'submission-claims').glob('*.json'))),1)
    def test_worker_bound_tracker_and_folder(self):
        import os
        row={'Company':'Acme','Role':'Java','Job ID':'A-1','Direct link':'https://acme.example/jobs/A-1'}
        with patch.dict(os.environ,{'JOB_SEARCH_WORKER_SLUG':'acme-a1','JOB_SEARCH_WORKER_COMPANY':'acme',
                                    'JOB_SEARCH_WORKER_REQ':'a1','JOB_SEARCH_WORKER_URL':row['Direct link']}):
            w.upsert({**row,'Status':'Shortlisted'},'bound test')
            with self.assertRaises(ValueError):w.job_folder('different')
            with self.assertRaises(ValueError):w.upsert({'Company':'Other','Job ID':'B-2','Status':'Shortlisted'},'cross-worker test')
            with self.assertRaises(ValueError):w.upsert({**row,'Direct link':'https://other.example/jobs/A-1'},'URL crossing test')
            w.worker_url_guard('https://forms.acme.example/apply/A-1')
            with self.assertRaises(ValueError):w.worker_url_guard('https://other.example/apply/A-1')

if __name__=='__main__': unittest.main()
