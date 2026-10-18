import contextlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
sys.path.insert(0,str(ROOT/'tests'))
import job_config,job_db,recovery
import naukri_native_worker as worker
from naukri_phase_guard import NaukriPhaseGuard,APPLY_PATH,CHAT_PATH
from test_auto_pipeline import Fixture,NEWGEN

REQ='160926009207'
URL='https://www.naukri.com/x'+REQ
def show():return {'flowtype':'show','strJobsarr':json.dumps([REQ])}

def claimed_guard():
    g=NaukriPhaseGuard(REQ)
    g.claim_before_open('attempt-1')
    ok,ev=g.decide('POST',URL.replace('/x'+REQ,APPLY_PATH),show());assert ok
    g.response(ev,200,{'job_id_match':True,'dataCommitted':False})
    g.claim('attempt-1');g.arm_save(action_label='Save',exact_chat=True,confirmed_answer=True)
    ok,ev=g.decide('POST','https://www.naukri.com'+CHAT_PATH,{'input':{'text':'x'}});assert ok
    g.response(ev,200,{'job_id_match':True})
    return g

class GuardReopenTests(unittest.TestCase):
    def test_reopen_needs_a_claim_and_a_saved_answer(self):
        g=NaukriPhaseGuard(REQ);self.assertFalse(g.may_reopen())
        with self.assertRaises(RuntimeError):g.arm_reopen()
    def test_same_claimed_attempt_can_reopen_with_one_show_then_answer_again(self):
        g=claimed_guard();self.assertTrue(g.may_reopen())
        g.arm_reopen()
        ok,ev=g.decide('POST','https://www.naukri.com'+APPLY_PATH,show());self.assertTrue(ok);self.assertEqual(ev['reason'],'reopen_same_claimed_attempt_show')
        g.response(ev,200,{'job_id_match':True,'dataCommitted':False});self.assertEqual(g.phase,'followup_status_received')
        g.arm_save(action_label='Save',exact_chat=True,confirmed_answer=True) if g.save_arm_count==g.answer_count else None
    def test_reopen_only_allows_a_show_for_this_job_and_is_bounded(self):
        g=claimed_guard();g.arm_reopen()
        ok,_=g.decide('POST','https://www.naukri.com'+APPLY_PATH,{'flowtype':'apply','strJobsarr':json.dumps([REQ])})
        self.assertFalse(ok)
        g=claimed_guard();g.arm_reopen()
        ok,_=g.decide('POST','https://www.naukri.com'+APPLY_PATH,{'flowtype':'show','strJobsarr':json.dumps(['999999999999'])})
        self.assertFalse(ok)
        g=claimed_guard()
        for _ in range(2):
            g.arm_reopen();g.phase='answer_sent'
        self.assertFalse(g.may_reopen())
    def test_no_reopen_after_commit(self):
        g=claimed_guard();g.workflow_committed=True;self.assertFalse(g.may_reopen())

class ReopenHelperTests(unittest.TestCase):
    def fake_page(self,guard,prompts):
        class Loc:
            def __init__(s,sel):s.sel=sel
            def filter(s,**k):return s
            def click(s,**k):
                ok,ev=guard.decide('POST','https://www.naukri.com'+APPLY_PATH,show())
                if ok:guard.response(ev,200,{'job_id_match':True,'dataCommitted':False})
        class Page:
            def locator(s,sel):return Loc(sel)
            def wait_for_timeout(s,ms):pass
        return Page()
    def test_reopen_continues_when_listing_is_live_and_chat_shows_next_question(self):
        g=claimed_guard();page=self.fake_page(g,['Q2'])
        with patch.object(worker,'_listing',return_value={'exact_path':True,'apply_count':1,'applied':False,'pending':False}),\
             patch.object(worker,'_chat',return_value={'drawer':True,'prompts':['Q2'],'thank_you':False}):
            self.assertTrue(worker._reopen(page,g,URL,REQ))
        self.assertEqual(g.reopen_count,1)
    def test_reopen_refused_when_listing_already_applied_or_not_unique(self):
        g=claimed_guard();page=self.fake_page(g,[])
        with patch.object(worker,'_listing',return_value={'exact_path':True,'apply_count':0,'applied':True,'pending':False}):
            self.assertFalse(worker._reopen(page,g,URL,REQ))
        self.assertEqual(g.reopen_count,0)

class RecoveryTests(Fixture):
    def test_interrupted_unclaimed_jobs_return_to_prepared_and_claims_stay_locked(self):
        a=self.job(NEWGEN);b=self.job(NEWGEN.replace('Bengaluru','Pune'),portal='170926009999',employer='Other Co')
        self.db.execute("UPDATE jobs SET state='Preparing',updated_at='2026-01-01T00:00:00+00:00' WHERE id=?",(a,))
        self.db.execute("UPDATE jobs SET state='SubmissionUnconfirmed',updated_at='2026-01-01T00:00:00+00:00' WHERE id=?",(b,))
        self.db.execute("INSERT INTO submission_attempts VALUES('att',?,'k','r','0','u','f','0','uncertain','2026-01-01T00:00:00+00:00',NULL,NULL,NULL)",(b,))
        self.db.execute("INSERT INTO queue(job_id,priority,queued_at,state,stage_started) VALUES(?,1,'2026-01-01','Resuming','2026-01-01T00:00:00+00:00')",(a,))
        report=recovery.recover_jobs(self.db)
        self.assertEqual((report['returned_to_prepared'],report['requeued_resume'],report['claimed_left_locked']),(1,1,1))
        self.assertEqual(self.db.execute('SELECT state FROM jobs WHERE id=?',(a,)).fetchone()[0],'Prepared')
        self.assertEqual(self.db.execute('SELECT state FROM jobs WHERE id=?',(b,)).fetchone()[0],'SubmissionUnconfirmed')
        self.assertEqual(self.db.execute('SELECT state FROM queue WHERE job_id=?',(a,)).fetchone()[0],'QueuedResume')

class RestoreRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=Path(tempfile.mkdtemp());self.addCleanup(shutil.rmtree,self.tmp,True);(self.tmp/'.workflow').mkdir()
        (self.tmp/'.workflow/profile-restore-pending.json').write_text('{}')
    def chrome(self,current):
        @contextlib.contextmanager
        def fake(headless=False):
            page=type('P',(),{})();yield type('C',(),{'pages':[page],'new_page':lambda s:page})()
        return fake
    def test_marker_triggers_restore_and_is_cleared_only_after_verification(self):
        uploads=[];names=['Yugal_Teotia_Backend_Java_Developer.pdf']
        def upload(page,pdf):uploads.append(pdf.name);names[0]=pdf.name;return {'after':pdf.name}
        with patch.object(worker,'ROOT',self.tmp),patch.object(worker,'_chrome',self.chrome(None)),\
             patch.object(worker,'_status',lambda p:{'signed_in':True}),patch.object(worker,'current_filename',lambda p:names[0]),\
             patch.object(worker,'upload_profile',upload):
            self.assertEqual(worker.recover_pending_restore()['status'],'Restored')
        self.assertEqual(uploads,['Yugal_Teotia_CV.pdf']);self.assertFalse((self.tmp/'.workflow/profile-restore-pending.json').exists())
    def test_failed_recovery_keeps_marker(self):
        with patch.object(worker,'ROOT',self.tmp),patch.object(worker,'_chrome',self.chrome(None)),\
             patch.object(worker,'_status',lambda p:{'signed_in':False}):
            self.assertEqual(worker.recover_pending_restore()['status'],'Failed')
        self.assertTrue((self.tmp/'.workflow/profile-restore-pending.json').exists())
    def test_pending_marker_blocks_worker_and_pause_stops_before_any_candidate(self):
        config=job_config.load();config['naukri_worker']['enabled']=True
        with patch.object(worker,'ROOT',self.tmp),patch.object(job_db,'naukri_worker_path_verified',return_value=True):
            self.assertEqual(worker.run(None,config,test_mode=True)['blocker'],'profile_restore_pending')
        (self.tmp/'.workflow/profile-restore-pending.json').unlink()
        with patch.object(worker,'ROOT',self.tmp),patch.object(job_db,'naukri_worker_path_verified',return_value=True),\
             patch.object(worker,'candidates',return_value=[{'id':1}]),patch.object(worker,'resumable',return_value=[]),patch('control.is_paused',return_value=True),\
             patch.object(worker,'_run_one') as run_one:
            (self.tmp/'APPLICATION_PROFILE.md').write_text('CONFIRMED')
            outcome=worker.run(None,config,test_mode=False)
        run_one.assert_not_called();self.assertEqual(outcome['results'][0]['status'],'Paused')

if __name__=='__main__':unittest.main()
