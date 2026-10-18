import json,sys,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'tools'),str(Path(__file__).resolve().parent)]
import naukri_native_worker as w,job_db
from naukri_direct_playwright import _playwright
from test_questionnaire_loop import LoopBase,Env,Chat,TWO,REQ

class ExactControls(unittest.TestCase):
    def test_sidebar_applied_is_not_exact_listing_confirmation(self):
        with _playwright()() as pw:
            browser=pw.chromium.launch(channel='chrome',headless=True)
            page=browser.new_page()
            html=['']
            page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=html[0]))
            cases=[('<button id="apply-button">Apply</button><aside>Applied</aside>',False),
                   ('<div class="styles_jhc__apply-button-container__x"><span id="already-applied">Applied</span></div>',True),
                   ('<aside><span id="already-applied">Applied</span></aside>',False),
                   ('<div class="styles_jhc__apply-button-container__x"><span id="already-applied">Applied</span><button id="apply-button">Apply</button></div>',False)]
            for markup,expected in cases:
                html[0]=markup;page.goto('https://www.naukri.com/job-listings-fixture-200826029442')
                self.assertEqual(page.evaluate(w.LISTING_STATE_SCRIPT,'200826029442')['applied'],expected)
                self.assertFalse(page.evaluate(w.LISTING_STATE_SCRIPT,'200826029266')['applied'])
            browser.close()

class Polling(unittest.TestCase):
    def test_delayed_history_reads_do_not_replay_actions_or_accept_partial_history(self):
        page=MagicMock();listing={'exact_path':True,'applied':True}
        histories=[{'full':False,'exact':1},{'full':True,'exact':0},{'full':True,'exact':1}]
        with patch.object(w,'_listing',return_value=listing),patch.object(w,'_history',side_effect=histories):
            end,history,observations=w.confirmation_observations(page,'url','id','company','title')
        self.assertEqual(len(observations),3);self.assertTrue(w.history_match(history))
        self.assertEqual(page.method_calls,[unittest.mock.call.wait_for_timeout(15000),unittest.mock.call.wait_for_timeout(15000)])
    def test_genuine_wall_stops_before_reading_history(self):
        with patch.object(w,'_listing',return_value={'site_restriction':True}),patch.object(w,'_history') as history:
            with self.assertRaisesRegex(RuntimeError,'CAPTCHA'):w.confirmation_observations(MagicMock(),'url','id','company','title')
            history.assert_not_called()

class PausedReconciliation(LoopBase):
    def proof(self):
        outcome=self.run_one(Env(Chat([TWO[0]]),applies_on_done=False))
        self.assertEqual(outcome['status'],'Uncertain')
        job=self.db.execute('SELECT * FROM jobs WHERE id=?',(self.job_id,)).fetchone();stamp=job_db.now()
        proof={'attempt_id':outcome['attempt_id'],'requisition_id':REQ,'captured_at':stamp,'url':job['canonical_url'],
            'confirmation_text':'Exact Application Sent server history','exact_history_id_match':True,'history_full':True,
            'new_application_action':False,'account_identity_verified':True,
            'history_identity':{'basis':'server_history_exact_job_id','server_rows':[{'job_id':REQ,'status':[{'statusValue':'Application Sent'}]}]}}
        path=self.folder()/'proof.json';path.write_text(json.dumps(proof));self.config['portal_workers']['naukri']['apply_enabled']=False
        return outcome,stamp,path
    def test_exact_server_proof_can_reconcile_while_submission_lane_paused(self):
        outcome,stamp,path=self.proof()
        with patch.object(job_db,'ROOT',self.tmp),patch.object(job_db,'naukri_worker_path_verified',return_value=True):
            job_db.confirm_attempt(self.db,outcome['attempt_id'],self.job_id,str(path.relative_to(self.tmp)),stamp,config=self.config,naukri_worker=True,reconcile_only=True)
        self.assertEqual(self.job_state(),'Applied');self.assertEqual(self.attempt()['state'],'confirmed')
    def test_wrong_exact_server_id_cannot_reconcile_while_paused(self):
        outcome,stamp,path=self.proof();proof=json.loads(path.read_text());proof['history_identity']['server_rows'][0]['job_id']='wrong';path.write_text(json.dumps(proof))
        with patch.object(job_db,'ROOT',self.tmp),patch.object(job_db,'naukri_worker_path_verified',return_value=True),self.assertRaisesRegex(ValueError,'exact server'):
            job_db.confirm_attempt(self.db,outcome['attempt_id'],self.job_id,str(path.relative_to(self.tmp)),stamp,config=self.config,naukri_worker=True,reconcile_only=True)
        self.assertEqual(self.attempt()['state'],'uncertain')

if __name__=='__main__':unittest.main()
