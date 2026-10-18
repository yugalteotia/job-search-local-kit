import contextlib,datetime as dt,fcntl,json,sys
from pathlib import Path
from unittest.mock import patch,MagicMock
import unittest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'tools'),str(Path(__file__).resolve().parent)]
import outcome_reconcile as r,job_db
from test_questionnaire_loop import LoopBase,Env,Chat,TWO,REQ

class Reconciliation(LoopBase):
    def make_pending(self,incomplete=False):
        result=self.run_one(Env(Chat(TWO if incomplete else [TWO[0]]),applies_on_done=incomplete))
        self.assertEqual(result['status'],'NeedsAnswer' if incomplete else 'Uncertain')
        return result
    def history(self,full=True,identifier=REQ):
        return {'full':full,'account_verified':True,'loaded_rows':1,'expected_total':1,
                'rows_by_id':{REQ:[{'job_id':identifier,'status':[{'statusValue':'Application Sent','dateTime':job_db.now()}]}]}}
    def run_reconcile(self,history,phase='before',test=False):
        with patch.object(r,'ROOT',self.tmp),patch.object(job_db,'ROOT',self.tmp),\
             patch.object(r,'naukri_history',return_value=history) as reader,\
             patch.object(r,'refresh_tracker',return_value={}),\
             patch('urgent_alerts.resolve_sqlite_confirmed_blocker'),\
             patch('urgent_alerts.desktop',return_value={'notification_sent':True}) as notify:
            out=r.run(self.db,self.config,phase=phase,test_mode=test,sources=('naukri',))
            return out,reader.call_count,notify.call_count
    def test_positive_history_confirms_original_claim_and_notifies_once(self):
        original=self.make_pending();out,reads,notices=self.run_reconcile(self.history())
        self.assertEqual(len(out['confirmed']),1);self.assertEqual(notices,1);self.assertEqual(out['new_claims'],0)
        self.assertEqual(self.attempt()['attempt_id'],original['attempt_id']);self.assertEqual(self.attempt()['state'],'confirmed')
        self.assertEqual(self.db.execute('SELECT count(*) FROM submission_attempts').fetchone()[0],1)
        out,reads,notices=self.run_reconcile(self.history(),'after');self.assertEqual(reads,0);self.assertEqual(notices,0)
    def test_incomplete_attempt_reconciles_without_an_answer_or_new_claim(self):
        original=self.make_pending(incomplete=True);out,reads,notices=self.run_reconcile(self.history())
        self.assertEqual(len(out['confirmed']),1);self.assertEqual(self.job_state(),'Applied')
        self.assertEqual(self.attempt()['attempt_id'],original['attempt_id']);self.assertEqual(self.db.execute('SELECT count(*) FROM submission_attempts').fetchone()[0],1)
    def test_partial_wrong_id_wrong_account_and_old_history_preserve_lock(self):
        self.make_pending()
        cases=[self.history(full=False),self.history(identifier='different'),{**self.history(),'account_verified':False}]
        old=self.history();old['rows_by_id'][REQ][0]['status'][0]['dateTime']='2020-01-01T00:00:00+00:00';cases.append(old)
        for h in cases:
            out,reads,notices=self.run_reconcile(h)
            self.assertEqual(out['confirmed'],[]);self.assertEqual(notices,0);self.assertEqual(self.attempt()['state'],'uncertain')
    def test_dry_run_never_opens_browser_or_changes_lock(self):
        self.make_pending();out,reads,notices=self.run_reconcile(self.history(),test=True)
        self.assertEqual(reads,0);self.assertEqual(notices,0);self.assertEqual(self.attempt()['state'],'uncertain')
    def test_previously_applied_without_claim_is_imported_without_inventing_attempt(self):
        self.db.execute("UPDATE jobs SET state='NeedsAnswer' WHERE id=?",(self.job_id,))
        out,reads,notices=self.run_reconcile(self.history())
        self.assertEqual(len(out['confirmed']),1);self.assertEqual(notices,1);self.assertEqual(self.job_state(),'Applied')
        self.assertEqual(self.db.execute('SELECT count(*) FROM submission_attempts').fetchone()[0],0)
        row=self.db.execute('SELECT tracker_json FROM jobs WHERE id=?',(self.job_id,)).fetchone()
        self.assertIn('Submission date',json.loads(row['tracker_json']))
    def test_notification_failure_is_durable_and_retried_without_history_or_application(self):
        self.make_pending()
        with patch.object(r,'ROOT',self.tmp),patch.object(job_db,'ROOT',self.tmp),patch.object(r,'naukri_history',return_value=self.history()),\
             patch.object(r,'refresh_tracker',return_value={}),patch('urgent_alerts.resolve_sqlite_confirmed_blocker'),\
             patch('urgent_alerts.desktop',return_value={'notification_sent':False}):
            out=r.run(self.db,self.config,phase='before',sources=('naukri',))
        self.assertEqual(self.attempt()['state'],'confirmed')
        out,reads,notices=self.run_reconcile(self.history(),'after');self.assertEqual(reads,0);self.assertEqual(notices,1)

class LinkedInReconciliation(LoopBase):
    def test_linkedin_requires_full_filtered_exact_history_and_retains_claim(self):
        self.db.execute("UPDATE jobs SET source='linkedin_portal_native',canonical_url=?,requisition_id=?,requisition_key=?,state='SubmissionUnconfirmed' WHERE id=?",('https://www.linkedin.com/jobs/view/4479000001/','4479000001','req:4479000001',self.job_id))
        self.db.execute("INSERT INTO submission_attempts VALUES('linkedin-existing',?,'req:4479000001','r','0','u','f','0','uncertain',?,NULL,NULL,NULL)",(self.job_id,job_db.now()))
        h={'full':True,'account_verified':True,'history_filter_verified':True,'loaded_rows':1,'expected_total':1,
           'rows_by_id':{'4479000001':[{'job_id':'4479000001','application_sent':True}]}}
        with patch.object(r,'ROOT',self.tmp),patch.object(job_db,'ROOT',self.tmp),patch.object(r,'linkedin_history') as reader,\
             patch.object(r,'refresh_tracker',return_value={}),patch('urgent_alerts.resolve_sqlite_confirmed_blocker'),\
             patch('urgent_alerts.desktop',return_value={'notification_sent':True}):
            for invalid in [{**h,'full':False},{**h,'history_filter_verified':False}]:
                reader.return_value=invalid;self.assertEqual(r.run(self.db,self.config,phase='before',sources=('linkedin',))['confirmed'],[])
            reader.return_value=h;out=r.run(self.db,self.config,phase='after',sources=('linkedin',))
        self.assertEqual(len(out['confirmed']),1);self.assertEqual(self.attempt()['attempt_id'],'linkedin-existing');self.assertEqual(self.job_state(),'Applied')

class SharedHooks(LoopBase):
    def test_exception_cleanup_reconciles_under_shared_lock_without_workers(self):
        import os,pipeline_v2,progress
        (self.tmp/'.workflow/runs').mkdir(parents=True,exist_ok=True)
        seen=[]
        def crash(*a,**k):
            os.environ['JOB_SEARCH_RUN_ID']='fixture-crashed-run'
            raise RuntimeError('fixture error before worker')
        def cleanup(db,config,**options):
            with (self.tmp/'.workflow/daily-run.lock').open('a') as probe:
                with self.assertRaises(BlockingIOError):fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
            seen.append(options['phase']);return {'confirmed':[]}
        class Connection:
            def __getattr__(s,n):return getattr(self.db,n)
            def close(s):pass
        with patch.object(pipeline_v2,'STATE',self.tmp/'.workflow'),patch.object(progress,'STATE',self.tmp/'.workflow'),\
             patch('job_config.load',return_value=self.config),patch.object(job_db,'connect',return_value=Connection()),\
             patch.object(pipeline_v2,'_locked',side_effect=crash),patch.object(r,'run',side_effect=cleanup),\
             patch.dict(os.environ,{'JOB_SEARCH_RUN_ID':'previous-fixture'}),self.assertRaises(RuntimeError):
            pipeline_v2.run(test=True,scheduled=True,manual=True,queue_only=True,sources=('naukri',))
        self.assertEqual(seen,['after_crash'])

    def test_timer_and_run_now_use_before_after_hooks_under_same_lock(self):
        import pipeline_v2,progress
        from portals import registry
        (self.tmp/'.workflow/runs').mkdir(parents=True,exist_ok=True)
        for manual in [False,True]:
            events=[]
            def reconcile(db,config,**options):
                with (self.tmp/'.workflow/daily-run.lock').open('a') as probe:
                    with self.assertRaises(BlockingIOError):fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
                events.append(options['phase']);return {'status':'SkippedDryRun','confirmed':[]}
            class Lane:
                def apply_lane(s,*a,**k):events.append('worker');return {'status':'DryRun','results':[],'submissions':0}
            class Connection:
                def __getattr__(s,n):return getattr(self.db,n)
                def close(s):pass
            with patch.object(pipeline_v2,'ROOT',self.tmp),patch.object(pipeline_v2,'STATE',self.tmp/'.workflow'),patch.object(progress,'STATE',self.tmp/'.workflow'),\
                 patch.object(job_db,'ROOT',self.tmp),patch.object(job_db,'connect',return_value=Connection()),\
                 patch('job_config.load',return_value=self.config),patch.object(r,'run',side_effect=reconcile),\
                 patch.object(registry,'apply_lane_worker',return_value=(Lane(),'fixture')),patch.object(registry,'capabilities',return_value={}),\
                 patch.object(pipeline_v2.job_views,'regenerate',return_value={}),patch('urgent_alerts.sync_sqlite_blockers',return_value=[]):
                pipeline_v2.run(test=True,scheduled=True,manual=manual,queue_only=True,sources=('naukri',),max_seconds=30,review_limit=1)
            self.assertEqual(events,['before','worker','after'])

if __name__=='__main__':unittest.main()
