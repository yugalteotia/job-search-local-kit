import contextlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'));sys.path.insert(0,str(ROOT/'tests'))
import auto_fit,auto_resume,job_db,lead_resolution,recovery,response_reconcile
import naukri_native_worker as worker
from naukri_phase_guard import APPLY_PATH,CHAT_PATH
from test_auto_pipeline import Fixture,NEWGEN,record

REQ='160926009207'
PROFILE=(ROOT/'APPLICATION_PROFILE.md').read_text()

class Chat:
    """Scripted Naukri chat: questions are (text, 'text'|'choice'). Advances on Save unless told to stall."""
    def __init__(self,questions,start=0,stall_after=None,close_between=False):
        self.q=questions;self.i=start;self.answer=None;self.stall_after=stall_after;self.saves=[]
        self.close_between=close_between;self.between=False
    @property
    def done(self):return self.i>=len(self.q)
    def snapshot(self):
        cur=None if self.done else self.q[self.i]
        options=[{'text':t,'id':t,'checked':self.answer==t} for t in ('Yes','No')] if cur and cur[1]=='choice' else []
        return {'drawer':True,'prompts':[cur[0]] if cur else [self.q[-1][0]],'options':options,
                'editor_count':1 if cur and cur[1]=='text' else 0,'editor_empty':[True],'save_count':0 if self.done else 1,
                'save_label':'Save','save_disabled':False,'thank_you':self.done or self.between}

class Env:
    """Fake browser + Naukri network wired to the real guard, routes and response callbacks."""
    def __init__(self,chat,*,applies_on_done=True,history_full=True,history_full_after_save=True):
        self.chat=chat;self.applies_on_done=applies_on_done;self.history_full=history_full;self.history_full_after_save=history_full_after_save
        self.route=None;self.on_response=None;self.blocked=[];self.posts=[];self.uploads=[]
    def net(self,path,payload):
        env=self
        class Req:method='POST';url='https://www.naukri.com'+path;post_data_json=payload
        class Route:
            request=Req;allowed=None
            def continue_(s):s.allowed=True
            def abort(s):s.allowed=False
        route=Route();self.route(route);self.posts.append((path,route.allowed))
        if route.allowed is False:self.blocked.append(path);return
        class Resp:
            url=Req.url;status=200
            def json(s):return {'chatbotResponse':{'isApply':True,'isLeafNode':False,'dataCommitted':False,'currentNodeName':'n'},'jobs':[REQ]}
        self.on_response(Resp())
    @property
    def applied(self):return self.applies_on_done and self.chat.done
    def listing(self,*a):return {'exact_path':True,'apply_count':1,'applied':self.applied,'pending':False}
    def history(self,*a):return {'full':self.history_full_after_save if self.chat.saves else self.history_full,'exact':1 if self.applied else 0,'company_role':0,'at':'now'}
    def page(self):
        env=self
        class Loc:
            def __init__(s,sel):s.sel=sel
            def filter(s,**k):return s
            def count(s):return 1
            def click(s,**k):
                if '#apply-button' in s.sel:
                    env.chat.between=False;env.net(APPLY_PATH,{'flowtype':'show','strJobsarr':json.dumps([REQ])})
                elif 'sendMsgbtn' in s.sel:
                    env.chat.saves.append(env.chat.q[env.chat.i][0])
                    env.net(CHAT_PATH,{'input':{'text':[env.chat.answer]}})
                    if env.chat.stall_after is not None and len(env.chat.saves)>env.chat.stall_after:return
                    env.chat.i+=1;env.chat.answer=None;env.chat.between=env.chat.close_between and not env.chat.done
                elif s.sel.startswith('.chatbot_Drawer label[for='):env.chat.answer=s.sel.split('"')[1]
            def fill(s,text,**k):env.chat.answer=text
            def inner_text(s):return env.chat.answer or ''
            def is_checked(s):return env.chat.answer is not None
        class Page:
            def on(s,event,fn):env.on_response=fn
            def locator(s,sel):return Loc(sel)
            def wait_for_timeout(s,ms):pass
            def evaluate(s,script,arg=None):return True
        return Page()

class LoopBase(Fixture):
    def setUp(self):
        super().setUp()
        (self.tmp/'.workflow').mkdir(exist_ok=True)
        self.job_id=self.job(NEWGEN);self.config['naukri_worker']['enabled']=True
        self.config['mode']='production';self.config['submissions']['enabled']=True
        self.config['portal_workers']['naukri']['apply_enabled']=True
        auto_fit.run(self.db,self.config)
        with patch.object(auto_resume,'ROOT',self.tmp),patch.object(job_db,'ROOT',self.tmp):
            auto_resume.run(self.db,self.config)
    def run_one(self,env,resume=None,profile=PROFILE,paused=False,startup=None):
        page=env.page()
        @contextlib.contextmanager
        def session(state):
            class Ctx:
                pages=[page]
                def new_page(s):return page
                def route(s,pattern,handler):env.route=handler
                def unroute_all(s,**k):pass
            try:yield Ctx()
            finally:
                if state.get('restore_needed'):
                    state['restored']=True;state['restore_sha256']=worker._sha(worker.GENERIC);worker._marker().unlink(missing_ok=True)
        row=self.db.execute('SELECT * FROM jobs WHERE id=?',(self.job_id,)).fetchone()
        patches=[patch.object(worker,'ROOT',self.tmp),patch.object(job_db,'ROOT',self.tmp),
                 patch.object(worker,'_browser_session',session),patch.object(worker,'_status',lambda p:{'signed_in':True}),patch('naukri_direct_playwright._status',lambda p:{'signed_in':True}),
                 patch.object(worker,'current_filename',lambda p:worker.GENERIC.name),
                 patch.object(worker,'ensure_generic_resume',return_value=startup or {'expected_sha256':worker._sha(worker.GENERIC),'verified_at':'fixture','repair_performed':False}),
                 patch.object(worker,'upload_profile',lambda p,pdf:{'after':pdf.name}),
                 patch.object(worker,'_listing',env.listing),patch.object(worker,'_history',env.history),
                 patch.object(worker,'_chat',lambda p:env.chat.snapshot()),
                 patch.object(job_db,'naukri_worker_path_verified',return_value=True),patch('control.is_paused',return_value=paused)]
        with contextlib.ExitStack() as stack:
            for p in patches:stack.enter_context(p)
            return worker._run_one(self.db,row,self.config,profile,False,resume=resume)
    def job_state(self):return self.db.execute('SELECT state FROM jobs WHERE id=?',(self.job_id,)).fetchone()[0]
    def attempt(self):return self.db.execute('SELECT * FROM submission_attempts WHERE job_id=?',(self.job_id,)).fetchone()
    def folder(self):return self.tmp/'jobs'/self.db.execute('SELECT slug FROM jobs WHERE id=?',(self.job_id,)).fetchone()[0]

TWO=[('What is your notice period?','text'),('Do you hold an active security clearance?','text')]

class SafelyIncompleteTests(LoopBase):
    def test_successful_startup_repair_continues_same_job_to_confirmation(self):
        result=self.run_one(Env(Chat([TWO[0]])),startup={'expected_sha256':worker._sha(worker.GENERIC),'verified_at':'fixture','repair_performed':True})
        self.assertEqual(result['status'],'Applied');self.assertTrue(result['generic_resume_repaired_at_start'])
        self.assertEqual(self.db.execute('select count(*) from submission_attempts').fetchone()[0],1)
    def test_preclaim_history_failure_has_actionable_saved_error_and_no_submission(self):
        env=Env(Chat(TWO),history_full=False)
        result=self.run_one(env)
        self.assertEqual(result['status'],'Blocked')
        self.assertEqual(result['stage'],'applied_history_check')
        self.assertEqual(result['error_message'],'Applied Jobs history is incomplete; load the full history before applying')
        saved=json.loads((self.folder()/'naukri-native-outcome.json').read_text())
        self.assertEqual(saved['error_message'],result['error_message'])
        self.assertIsNone(self.attempt());self.assertFalse(env.posts);self.assertFalse(env.chat.saves)

    def test_thank_you_between_questions_continues_same_claim_without_replaying_answer(self):
        env=Env(Chat([('What is your notice period?','text'),('What is your Current location?','text')],close_between=True))
        result=self.run_one(env)
        self.assertEqual(result['status'],'Applied',result)
        self.assertEqual(len(env.chat.saves),2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM submission_attempts').fetchone()[0],1)
    def test_unknown_next_question_after_a_saved_answer_is_needs_answer_not_uncertain(self):
        env=Env(Chat(TWO));result=self.run_one(env)
        self.assertEqual((result['status'],result['blocker'],result['incomplete']),('NeedsAnswer','unknown_required_answer',True))
        self.assertEqual(result['question'],TWO[1][0])
        self.assertEqual(self.job_state(),'NeedsAnswer');self.assertEqual(self.attempt()['state'],'incomplete')
        self.assertEqual(env.chat.saves,[TWO[0][0]]);self.assertEqual(env.blocked,[])
        progress=json.loads((self.folder()/'naukri-native-progress.json').read_text())
        self.assertEqual((progress['status'],progress['saves'],progress['pending_question'],progress['save_in_flight']),('incomplete',1,TWO[1][0],False))
        self.assertNotIn('Immediate joiner',json.dumps(progress))
        outcome=json.loads((self.folder()/'naukri-native-outcome.json').read_text());self.assertEqual(outcome['question'],TWO[1][0])
        evidence=json.loads((self.folder()/'naukri-native-incomplete-evidence.json').read_text())
        self.assertTrue(evidence['safe']);self.assertEqual((evidence['completion_count'],evidence['history_exact']),(0,0))
    def test_unsupported_format_and_answer_not_among_options_also_stop_safely(self):
        env=Env(Chat([('What is your notice period?','text'),('Do you know Kafka?','choice')]));result=self.run_one(env)
        self.assertEqual(result['blocker'],'unknown_required_answer');self.assertEqual(self.attempt()['state'],'incomplete')
    def test_choice_question_is_answered_from_confirmed_rule_and_matches_the_payload(self):
        env=Env(Chat([('Are you comfortable with Work from office role for Bangalore location?','choice')]))
        result=self.run_one(env);self.assertEqual(result['status'],'Applied',result)
        self.assertEqual(env.chat.saves,['Are you comfortable with Work from office role for Bangalore location?'])
    def test_unsafe_evidence_keeps_the_attempt_uncertain(self):
        env=Env(Chat(TWO),history_full_after_save=False);result=self.run_one(env)
        self.assertEqual(result['status'],'Uncertain');self.assertEqual(self.attempt()['state'],'uncertain')
        self.assertEqual(self.job_state(),'SubmissionUnconfirmed')
    def test_ambiguous_save_outcome_is_uncertain_never_incomplete(self):
        env=Env(Chat([TWO[0]],stall_after=0));result=self.run_one(env)
        self.assertEqual(result['status'],'Uncertain');self.assertEqual(self.attempt()['state'],'uncertain')
        progress=json.loads((self.folder()/'naukri-native-progress.json').read_text());self.assertTrue(progress['save_in_flight'])
    def test_full_questionnaire_with_positive_proof_becomes_applied(self):
        env=Env(Chat([TWO[0],('What is your Current location?','text')]));result=self.run_one(env)
        self.assertEqual(result['status'],'Applied',result);self.assertEqual(self.job_state(),'Applied')
        self.assertEqual(self.attempt()['state'],'confirmed');self.assertTrue((self.folder()/'naukri-native-confirmation.json').is_file())
        self.assertTrue(result['generic_resume_restored'])
    def test_no_positive_proof_means_uncertain_even_after_thank_you(self):
        env=Env(Chat([TWO[0]]),applies_on_done=False);result=self.run_one(env)
        self.assertEqual(result['status'],'Uncertain');self.assertNotEqual(self.job_state(),'Applied')

class ContinuationTests(LoopBase):
    def test_delayed_exact_history_reconciles_empty_answers_without_replay(self):
        attempt=self.stop_at_unknown()
        with patch.object(worker,'ROOT',self.tmp):
            worker._save_progress(self.folder(),attempt,REQ,[],'Unknown pending question',status='incomplete')
            progress=worker._load_progress(self.folder())
        chat=Chat([],start=0);env=Env(chat)
        result=self.run_one(env,resume=progress)
        self.assertEqual(result['status'],'Applied',result)
        self.assertFalse(result['questionnaire_completion_verified'])
        self.assertFalse(result['new_application_action'])
        self.assertEqual(env.posts,[]);self.assertEqual(env.uploads,[])
        self.assertEqual(self.attempt()['state'],'confirmed')
        self.assertEqual(self.db.execute('SELECT count(*) FROM submission_attempts').fetchone()[0],1)
        proof=json.loads((self.folder()/'naukri-native-confirmation.json').read_text())
        self.assertEqual(proof['unverified_pending_question'],'Unknown pending question')

    def test_partial_delayed_history_stays_locked_without_positive_claim(self):
        attempt=self.stop_at_unknown()
        with patch.object(worker,'ROOT',self.tmp):progress=worker._load_progress(self.folder())
        env=Env(Chat([]),history_full=False,history_full_after_save=False)
        result=self.run_one(env,resume=progress)
        self.assertEqual(result['status'],'Uncertain');self.assertTrue(result['reconciliation_pending'])
        self.assertEqual(self.attempt()['state'],'uncertain');self.assertEqual(self.job_state(),'SubmissionUnconfirmed')
        self.assertFalse((self.folder()/'naukri-native-confirmation.json').exists())
        self.assertEqual(env.posts,[])

    def stop_at_unknown(self):
        env=Env(Chat(TWO));self.run_one(env);self.assertEqual(self.attempt()['state'],'incomplete');return self.attempt()['attempt_id']
    def with_answerable(self):
        return PROFILE+'\n- Security clearance: no. Application answer: **No security clearance**\n'
    def test_resumable_needs_an_answerable_pending_question_and_a_bounded_number_of_resumes(self):
        attempt=self.stop_at_unknown()
        with patch.object(worker,'ROOT',self.tmp):
            self.assertEqual(worker.resumable(self.db,self.config,PROFILE),[])
            progress=worker._load_progress(self.folder())
            progress['pending_question']='What is your Current location?';worker._save_progress(self.folder(),attempt,REQ,progress['answered'],progress['pending_question'],status='incomplete')
            self.assertEqual(len(worker.resumable(self.db,self.config,PROFILE)),1)
            worker._save_progress(self.folder(),attempt,REQ,progress['answered'],progress['pending_question'],status='incomplete',resumes=worker.MAX_RESUMES)
            self.assertEqual(worker.resumable(self.db,self.config,PROFILE),[])
    def test_same_attempt_continues_answers_only_the_new_question_and_needs_positive_proof(self):
        attempt=self.stop_at_unknown()
        with patch.object(worker,'ROOT',self.tmp):
            progress=worker._load_progress(self.folder())
            progress['pending_question']='What is your Current location?'
            worker._save_progress(self.folder(),attempt,REQ,progress['answered'],progress['pending_question'],status='incomplete')
            (row,progress),=worker.resumable(self.db,self.config,PROFILE)
        chat=Chat([TWO[0],('What is your Current location?','text')],start=1)
        env=Env(chat);result=self.run_one(env,resume=progress)
        self.assertEqual(result['status'],'Applied',result);self.assertEqual(result['attempt_id'],attempt)
        self.assertEqual(chat.saves,['What is your Current location?'])           # earlier answer is never replayed
        self.assertEqual(self.db.execute('SELECT count(*) FROM submission_attempts WHERE job_id=?',(self.job_id,)).fetchone()[0],1)
        self.assertEqual(self.attempt()['state'],'confirmed');self.assertEqual(self.job_state(),'Applied')
    def test_continuation_that_stops_again_is_incomplete_again_and_counts_the_resume(self):
        attempt=self.stop_at_unknown()
        with patch.object(worker,'ROOT',self.tmp):
            progress=worker._load_progress(self.folder());progress['pending_question']='What is your Current location?'
        chat=Chat([TWO[0],('What is your Current location?','text'),('Any other unknown?','text')],start=1)
        env=Env(chat);result=self.run_one(env,resume=progress)
        self.assertEqual((result['status'],result.get('incomplete')),('NeedsAnswer',True));self.assertEqual(self.attempt()['state'],'incomplete')
        self.assertEqual(worker._load_progress(self.folder())['resumes'],1)
    def test_paused_workflow_leaves_the_attempt_incomplete_and_opens_nothing(self):
        self.stop_at_unknown()
        with patch.object(worker,'ROOT',self.tmp):
            progress=worker._load_progress(self.folder());progress['pending_question']='What is your Current location?'
        env=Env(Chat(TWO,start=1));result=self.run_one(env,resume=progress,paused=True)
        self.assertEqual(result['status'],'Paused');self.assertEqual(env.posts,[])
        self.assertEqual(self.attempt()['state'],'incomplete');self.assertEqual(self.job_state(),'NeedsAnswer')

class GateTests(LoopBase):
    def test_mark_incomplete_rejects_forged_or_unsafe_evidence_and_locked_uncertain_stays_locked(self):
        env=Env(Chat([TWO[0]],stall_after=0));self.run_one(env)               # ambiguous Save -> Uncertain
        attempt=self.attempt();self.assertEqual(attempt['state'],'uncertain')
        evidence=self.folder()/'naukri-native-incomplete-evidence.json'
        evidence.write_text(json.dumps({'attempt_id':attempt['attempt_id'],'requisition_id':REQ,'safe':True,'completion_count':0,
            'workflow_committed':False,'denied_events':0,'save_in_flight':True,'listing_applied':False,'history_full':True,
            'history_exact':0,'pending_question':'q','checked_at':job_db.now()}))
        with patch.object(job_db,'ROOT',self.tmp):
            with self.assertRaises(ValueError):job_db.mark_attempt_incomplete(self.db,attempt['attempt_id'],self.job_id,str(evidence.relative_to(self.tmp)))
        self.assertEqual(self.attempt()['state'],'uncertain')
    def test_resume_requires_incomplete_state_and_cannot_create_a_second_claim(self):
        with self.assertRaises(ValueError):job_db.resume_incomplete_attempt(self.db,'nope',self.job_id)
        env=Env(Chat(TWO));self.run_one(env)
        with self.assertRaises(ValueError):job_db.begin_attempt(self.db,self.job_id,'x','0'*64,'https://a','y','0'*64,config=self.config,naukri_worker=True)
    def test_interrupted_continuation_is_locked_as_unconfirmed(self):
        env=Env(Chat(TWO));self.run_one(env)
        attempt=self.attempt()['attempt_id'];job_db.resume_incomplete_attempt(self.db,attempt,self.job_id)
        self.assertEqual(self.job_state(),'InProgress')
        self.db.execute("UPDATE jobs SET updated_at='2026-01-01T00:00:00+00:00' WHERE id=?",(self.job_id,))
        report=recovery.recover_jobs(self.db);self.assertEqual(report['interrupted_continuations_locked'],1)
        self.assertEqual(self.job_state(),'SubmissionUnconfirmed');self.assertEqual(self.attempt()['state'],'uncertain')
    def test_incomplete_needs_answer_is_alerted_as_a_handoff(self):
        import urgent_alerts
        env=Env(Chat(TWO));self.run_one(env)
        sent=[]
        with patch.object(urgent_alerts,'STATE',self.tmp/'alerts'),patch.object(urgent_alerts,'desktop',lambda *a,**k:sent.append(a) or {'notification_sent':True,'notification_id':1}),\
             patch.object(urgent_alerts,'phone_config',return_value=None):
            (self.tmp/'alerts').mkdir(exist_ok=True)
            out=urgent_alerts.sync_sqlite_blockers(self.db,self.config)
        self.assertEqual(len(out),1);self.assertEqual(len(sent),1)

class DedupeTests(Fixture):
    def add(self,title,portal_id,*,state='Applied',req=None,employer='Infosys',portal='naukri'):
        rec=record(NEWGEN,title=title,portal=portal_id,employer=employer)
        rec['provenance']['portal']=portal;rec['requisition_id']=req;rec['slug']='infosys-'+portal_id
        job_id,_=job_db.create_job(self.db,rec)
        self.db.execute('UPDATE jobs SET state=? WHERE id=?',(state,job_id));return job_id
    def check(self,title,portal_id,**k):return lead_resolution._portal_duplicate_check(self.db,'Infosys',title,portal_id,None,portal='naukri',**k)
    def test_unapplied_sibling_leads_and_closed_history_never_block(self):
        self.add('Java Backend Developer',"110000000001",state='Verified')
        self.add('Java Backend Developer',"110000000002",state='Shortlisted')
        self.db.execute("INSERT INTO history_keys VALUES('infosys','req:x','Infosys','Java Backend Developer','x',NULL,'Closed','2026-01-01','r','t')")
        self.assertEqual(self.check('Java Backend Developer','110000000009')['status'],'clear')
    def test_different_exact_ids_are_different_postings_even_after_an_application(self):
        self.add('Java Backend Developer','110000000001',state='Applied')
        result=self.check('Java Backend Developer','110000000009')
        self.assertEqual(result['status'],'clear');self.assertEqual(result['compared_by_exact_id'],1)
    def test_unresolved_attempt_with_near_identical_title_still_blocks(self):
        job_id=self.add('Java Backend Developer','110000000001',state='SubmissionUnconfirmed')
        self.db.execute("INSERT INTO submission_attempts VALUES('a',?,'k','r','0','u','f','0','uncertain','2026-01-01T00:00:00+00:00',NULL,NULL,NULL)",(job_id,))
        self.assertEqual(self.check('Java Backend Developer','110000000009')['status'],'uncertain')
        self.assertEqual(self.check('Payments Platform Engineer','110000000009')['status'],'clear')
    def test_exact_portal_id_and_requisition_history_still_duplicate(self):
        self.add('Java Backend Developer','110000000001',state='Applied')
        self.assertEqual(self.check('Anything','110000000001')['status'],'exact_portal_id')
    def test_role_similarity_only_when_ids_cannot_be_compared(self):
        self.db.execute("INSERT INTO history_keys VALUES('infosys','fallback:z','Infosys','Java Backend Developer',NULL,NULL,'Rejected','2026-01-01','r','t')")
        self.assertEqual(self.check('Java Backend Developer','110000000009')['status'],'uncertain')
        self.assertEqual(self.check('Data Platform Analyst','110000000009')['status'],'clear')
    def test_history_known_distinct_portal_ids_do_not_block_by_title(self):
        self.db.execute("INSERT INTO history_keys VALUES('infosys','req:110826000033','Infosys','Java Backend Developer','110826000033','https://www.naukri.com/job-listings-java-backend-developer-110826000033','Applied','2026-01-01',NULL,'portal_observation')")
        for req in ('200826029442','200826029266','200826029374'):
            self.assertEqual(self.check('Java Backend Developer',req)['status'],'clear')
        self.assertEqual(self.check('Different title','110826000033')['status'],'exact_portal_id')

class MailGapTests(unittest.TestCase):
    def setUp(self):
        self.tmp=Path(tempfile.mkdtemp());self.addCleanup(shutil.rmtree,self.tmp,True);(self.tmp/'.workflow/reconcile').mkdir(parents=True)
    def gaps(self,items):(self.tmp/'.workflow/reconcile/history-gaps.json').write_text(json.dumps(items))
    def test_employer_only_evidence_never_blocks_a_different_role(self):
        self.gaps([{'schema':2,'employer_key':'vois','role':None,'requisition_id':None,'scope':'employer_only'}])
        self.assertFalse(response_reconcile.history_gap_blocks('vois','Java Backend Developer',self.tmp))
    def test_requisition_id_decides_when_present_on_both_sides(self):
        self.gaps([{'schema':2,'employer_key':'acme','role':'Java Developer','requisition_id':'R-1001','scope':'requisition'}])
        self.assertTrue(response_reconcile.history_gap_blocks('acme','Any title',self.tmp,requisition_id='r1001'))
        self.assertFalse(response_reconcile.history_gap_blocks('acme','Java Developer',self.tmp,requisition_id='R-2002'))
    def test_role_scope_blocks_only_the_same_role(self):
        self.gaps([{'schema':2,'employer_key':'acme','role':'Java Backend Developer','requisition_id':None,'scope':'role'}])
        self.assertTrue(response_reconcile.history_gap_blocks('acme','Backend Java Developer',self.tmp))
        self.assertTrue(response_reconcile.history_gap_blocks('acme','Java Back End Developer',self.tmp))
        self.assertFalse(response_reconcile.history_gap_blocks('acme','Java Developer',self.tmp))
        self.assertFalse(response_reconcile.history_gap_blocks('other','Java Backend Developer',self.tmp))
    def test_old_schema_entries_are_not_treated_as_employer_wide_blocks(self):
        self.gaps([{'employer_key':'acme','role_tokens':[]}])
        self.assertFalse(response_reconcile.history_gap_blocks('acme','Java Backend Developer',self.tmp))
    def test_role_and_requisition_extraction(self):
        r={'subject':'Your application to Java Software Engineer at Mphasis'}
        self.assertEqual(response_reconcile.role_from(r),'Java Software Engineer')
        self.assertIsNone(response_reconcile.role_from({'subject':'Track your VOIS Careers application'}))
        self.assertEqual(response_reconcile.requisition_from({'subject':'Update for requisition 2388999'}),'2388999')

if __name__=='__main__':unittest.main()
