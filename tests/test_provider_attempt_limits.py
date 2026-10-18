"""No-site checks for independent budgets and the simplified fit policy."""
import contextlib
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import application_budget as budgets
import auto_fit,candidate,fit_engine,job_config,job_db
import naukri_native_worker as nw
import linkedin_easy_apply as li
from test_auto_pipeline import NEWGEN,Fixture

class Budgets(unittest.TestCase):
    def worker(self,provider,stop=False,existing=False,dry=False):
        module=nw if provider=='naukri' else li
        cfg=job_config.load();claims={};calls=[]
        rows=[{'id':i,'slug':'fixture-'+str(i),'state':'ResumeReady'} for i in range(1,16)]
        if existing:claims[1]='existing-claim'
        def one(db,row,*args,**kw):
            i=row['id'];calls.append(i)
            if dry:return {'job_id':i,'status':'WouldSubmit','actual_submission':False}
            status='Blocked' if i==2 else 'Uncertain' if i==3 else 'Applied'
            if status!='Blocked':claims.setdefault(i,'claim-'+str(i))
            result={'job_id':i,'status':status,'attempt_id':claims.get(i),'generic_resume_restored':True}
            if stop and i==2:result['provider_stop_reason']='site_verification_restriction_or_limit'
            return result
        with tempfile.TemporaryDirectory() as temp,contextlib.ExitStack() as stack:
            root=Path(temp);(root/'APPLICATION_PROFILE.md').write_text('CONFIRMED fixture only')
            for m in (nw,li):
                stack.enter_context(patch.object(m,'ROOT',root))
                stack.enter_context(patch.object(m,'pace'))
                stack.enter_context(patch.object(m,'claim_id',side_effect=lambda db,i:claims.get(i)))
                stack.enter_context(patch.object(m,'_run_one',side_effect=one))
            stack.enter_context(patch.object(job_db,'portal_path_verified',return_value=True))
            stack.enter_context(patch.object(job_db,'naukri_worker_path_verified',return_value=True))
            stack.enter_context(patch('control.is_paused',return_value=False))
            stack.enter_context(patch.object(nw,'candidates',return_value=rows))
            stack.enter_context(patch.object(nw,'resumable',return_value=[]))
            stack.enter_context(patch.object(nw,'_note_block'))
            stack.enter_context(patch.object(nw,'save_live_receipt'))
            stack.enter_context(patch('urgent_alerts.resolve_sqlite_confirmed_blocker'))
            stack.enter_context(patch.object(li,'candidates',return_value=rows))
            stack.enter_context(patch.object(nw,'_chrome',side_effect=AssertionError('No browser allowed')))
            stack.enter_context(patch.object(li,'chrome',side_effect=AssertionError('No browser allowed')))
            result=module.run(MagicMock(),cfg,test_mode=dry,max_submissions=10)
        return result,calls,claims

    def test_both_providers_skip_unclaimed_preflight_and_count_uncertain_claims(self):
        for provider in ('naukri','linkedin'):
            result,calls,claims=self.worker(provider)
            self.assertEqual(len(calls),11);self.assertEqual(result['new_attempts_used'],10)
            self.assertEqual(result['new_claims_created'],10)
            self.assertFalse(result['results'][1]['new_attempt_slot_consumed'])
            self.assertTrue(result['results'][2]['new_attempt_slot_consumed'])
            self.assertFalse(result['provider_stopped'])

    def test_existing_claim_continuation_does_not_consume_a_new_slot(self):
        result,calls,claims=self.worker('naukri',existing=True)
        self.assertEqual(len(calls),12);self.assertEqual(result['new_attempts_used'],10)
        self.assertEqual(result['existing_attempts_continued'],1);self.assertEqual(claims[1],'existing-claim')

    def test_site_limit_stops_only_affected_worker(self):
        stopped,_,_=self.worker('linkedin',stop=True)
        unaffected,_,_=self.worker('naukri')
        self.assertEqual(stopped['new_attempts_used'],1);self.assertTrue(stopped['provider_stopped'])
        self.assertEqual(unaffected['new_attempts_used'],10)

    def test_dry_run_uses_no_attempt_slots_or_claims(self):
        for provider in ('naukri','linkedin'):
            result,_,claims=self.worker(provider,dry=True)
            self.assertFalse(claims);self.assertEqual(result.get('new_attempts_used',0),0)

    def test_changed_existing_claim_is_refused(self):
        budget=budgets.AttemptBudget(10)
        with self.assertRaisesRegex(RuntimeError,'original claim'):budget.record({},'old','new')

class PreviouslyApplied(Fixture):
    def proof(self,full=True,exact_id='070926029541'):
        job_id=self.job(NEWGEN.replace('160926009207','070926029541'),portal='070926029541',employer='Infosys')
        self.db.execute('UPDATE jobs SET requisition_id=? WHERE id=?',('070926029541',job_id))
        job=dict(self.db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
        folder=self.tmp/'jobs'/job['slug'];folder.mkdir(parents=True,exist_ok=True)
        path=folder/'observed.json'
        path.write_text(json.dumps({'requisition_id':job['requisition_id'],'url':job['canonical_url'],
            'captured_at':job_db.now(),'workflow_worker_proven':False,'history_full':full,'exact_history_id_match':True,
            'history_identity':{'basis':'server_history_exact_job_id','server_rows':[{'job_id':exact_id,'status':[{'statusValue':'Application Sent'}]}]}}))
        self.db.execute("INSERT INTO queue(job_id,priority,queued_at,state) VALUES(?,?,?,?)",(job_id,1,job_db.now(),'Blocked'))
        return job_id,str(path.relative_to(self.tmp))

    def test_full_exact_history_import_removes_queue_without_new_claim(self):
        job_id,path=self.proof()
        with patch.object(job_db,'ROOT',self.tmp):job_db.record_observed_naukri_application(self.db,job_id,path)
        self.assertEqual(self.db.execute('SELECT state FROM jobs WHERE id=?',(job_id,)).fetchone()[0],'Applied')
        self.assertEqual(self.db.execute('SELECT count(*) FROM queue WHERE job_id=?',(job_id,)).fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM submission_attempts').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM history_keys').fetchone()[0],1)

    def test_partial_history_or_wrong_exact_id_cannot_reconcile(self):
        for full,exact_id in [(False,'070926029541'),(True,'different')]:
            # Reset the independent fixture job before each variant.
            self.db.execute('DELETE FROM queue');self.db.execute('DELETE FROM state_events');self.db.execute('DELETE FROM jobs')
            job_id,path=self.proof(full,exact_id)
            with patch.object(job_db,'ROOT',self.tmp),self.assertRaises(ValueError):
                job_db.record_observed_naukri_application(self.db,job_id,path)
            self.assertEqual(self.db.execute('SELECT state FROM jobs WHERE id=?',(job_id,)).fetchone()[0],'Verified')

class Suitability(unittest.TestCase):
    def check(self,jd):
        row={'jd_text':jd,'title':'Java Backend Developer','location':'Bengaluru','availability':'Live','checked_at':'2026-10-13'}
        cfg=job_config.load();packet,reasons,_=auto_fit.build_packet(row,candidate.load(),cfg)
        return fit_engine.evaluate(row,packet,cfg) if packet else None,reasons

    def test_secondary_mandatory_technology_gaps_are_interview_preparation(self):
        jd=NEWGEN.replace('Job Requirements :','Job Requirements :\nMust have hands-on Kafka, Kubernetes and Redis experience.')
        result,reasons=self.check(jd);self.assertFalse(reasons)
        self.assertTrue(result['strong_medium_eligible']);self.assertFalse(result['eligible'])
        self.assertTrue(any(g['name']=='messaging' for g in result['interview_preparation_gaps']))

    def test_approved_three_year_near_miss_but_not_four_year_minimum(self):
        result,reasons=self.check(NEWGEN.replace('2 - 4 years','3 - 5 years').replace('2-4 years','3-5 years'))
        self.assertFalse(reasons);self.assertTrue(result['strong_medium_eligible'])
        result,reasons=self.check(NEWGEN.replace('2 - 4 years','4 - 6 years').replace('2-4 years','4-6 years'))
        self.assertIsNone(result);self.assertIn('Required experience starts at 4 years',reasons)

    def test_secondary_skill_tenure_does_not_replace_overall_experience_band(self):
        jd=NEWGEN.replace('Job Requirements :','Job Requirements :\nMust have five years of Kafka experience.')
        result,reasons=self.check(jd);self.assertFalse(reasons);self.assertTrue(result['strong_medium_eligible'])
        self.assertTrue(any('five years' in g.get('jd_excerpt','') for g in result['interview_preparation_gaps']))

    def test_different_primary_stack_and_missing_spring_are_excluded(self):
        import re
        for jd in (NEWGEN.replace('Job Requirements :','Job Requirements :\nPrimary stack: Python and Django.'),re.sub(r'spring\s*boot','Java REST APIs',NEWGEN,flags=re.I)):
            result,reasons=self.check(jd);self.assertIsNone(result);self.assertTrue(reasons)

    def test_compound_question_does_not_invent_secondary_skill_tenure(self):
        profile=(candidate.ROOT/'APPLICATION_PROFILE.md').read_text()
        self.assertEqual(candidate.answer('How many years of experience with Java and React?',profile),(None,None))
        self.assertEqual(candidate.answer('How many years of experience with Java and Kafka?',profile),(None,None))

class SharedDispatch(Fixture):
    def test_scheduled_and_manual_dispatch_each_independent_ten_attempt_cap_without_sites(self):
        import pipeline_v2,progress
        from portals import registry
        self.config['portal_workers']['linkedin'].update(apply_enabled=True,scheduled_apply_enabled=True)
        self.config['sources']['linkedin']['enabled']=True
        (self.tmp/'.workflow/runs').mkdir(parents=True,exist_ok=True)
        seen=[]
        class Lane:
            def __init__(self,name):self.name=name
            def apply_lane(self,db,config,**options):
                seen.append((self.name,options['max_submissions']))
                return {'status':'DryFixture','results':[],'submissions':0}
        for manual in (False,True):
            with patch.object(job_config,'load',return_value=self.config),patch.object(job_db,'connect',return_value=self.db),\
                 patch.object(pipeline_v2,'ROOT',self.tmp),patch.object(pipeline_v2,'STATE',self.tmp/'.workflow'),patch.object(progress,'STATE',self.tmp/'.workflow'),\
                 patch.object(registry,'apply_lane_worker',side_effect=lambda db,c,p,**k:(Lane(p),'fixture')),\
                 patch.object(pipeline_v2,'_run_source',side_effect=lambda section,name,*a:(section,{'source':name,'status':'Success','checked_at':job_db.now(),'pages':[],'leads':[],'jobs':[]})),\
                 patch('urgent_alerts.sync_sqlite_blockers',return_value=[]),\
                 patch.object(pipeline_v2.job_views,'regenerate',return_value={}):
                # Prevent the entrypoint closing this fixture's reusable connection.
                class Connection:
                    def __getattr__(s,n):return getattr(self.db,n)
                    def close(s):pass
                with patch.object(job_db,'connect',return_value=Connection()):
                    pipeline_v2.run(scheduled=True,manual=manual,test=True,sources=('naukri','linkedin'),queue_only=manual,review_limit=1,max_seconds=30)
        self.assertEqual(seen,[('naukri',10),('linkedin',10),('naukri',10),('linkedin',10)])

if __name__=='__main__':unittest.main()
