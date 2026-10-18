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
import auto_fit,auto_resume,candidate,job_config,job_db,master_check,quick_status,response_reconcile
import naukri_native_worker as worker

NEWGEN='''Location: Bengaluru
Experience: 2 - 4 years

Job highlights
2-4 years Java backend development experience with expertise in Spring Boot
Job description

Roles and Responsibilities :

Design, develop, test, deploy and maintain large-scale Java applications using Spring Boot.
Participate in code reviews to improve overall quality of the application.

Job Requirements :

2-4 years of experience in developing Java applications with a focus on backend development.
Strong understanding of design patterns (OOPs) and multithreading concepts.
Proficiency in database creation/management systems such as MySQL or similar technologies.
Role: Software Development - Other
Key Skills
JavaKafka
'''

def record(jd,title='Backend Java Developer',portal='160926009207',employer='Newgen',location='Bengaluru'):
    return {'employer':employer,'title':title,'canonical_url':f'https://www.naukri.com/job-listings-backend-java-developer-{portal}',
            'source':'naukri_portal_native','location':location,'discovered_at':job_db.now(),'checked_at':job_db.now(),
            'jd_text':jd,'availability':'Live','provenance':{'portal':'naukri','portal_job_id':portal,
            'verification_method':'portal_native','duplicate_check':{'status':'clear','possible_matches':[]}}}

class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp=Path(tempfile.mkdtemp());self.addCleanup(shutil.rmtree,self.tmp,True)
        shutil.copytree(ROOT/'master-resume',self.tmp/'master-resume')
        self.db=job_db.connect(self.tmp/'jobs.sqlite3');job_db.initialize(self.db)
        self.config=job_config.load();self.cand=candidate.load()
        # Historical strict-fit checks remain explicit; the current relaxed
        # secondary-skill policy has its own provider-limit regressions.
        self.config['high_fit']['secondary_skills_policy']='block'
        # This isolated fixture uses a synthetic copy of an old requisition ID.
        self.config['naukri_worker']['excluded_requisition_ids']=[x for x in self.config['naukri_worker']['excluded_requisition_ids'] if str(x)!='160926009207']
    def job(self,jd,**kw):
        job_id,_=job_db.create_job(self.db,record(jd,**kw))
        job_db.transition(self.db,job_id,'Verified','test','fixture',None,'Discovered')
        return job_id

class CandidateConfigTests(unittest.TestCase):
    def test_every_evidence_excerpt_exists_in_master_and_profiles(self):
        self.assertEqual(candidate.drift(),[])
    def test_answers_come_from_confirmed_profile_facts_only(self):
        profile=(ROOT/'APPLICATION_PROFILE.md').read_text()
        self.assertEqual(candidate.answer('What is your notice period?',profile)[0],'Immediate joiner')
        self.assertEqual(candidate.answer('What is your current CTC?',profile)[1],'last_annual_ctc')
        self.assertEqual(candidate.answer('What is your current CTC?','no facts here'),(None,None))
        self.assertEqual(candidate.answer('What is your notice period?','no facts here'),(None,None))

class AutoFitTests(Fixture):
    def test_unchanged_jd_cache_is_invalidated_when_candidate_context_changes(self):
        jd=NEWGEN.replace('Spring Boot','Quarkus').replace('Spring boot','Quarkus')
        self.job(jd)
        first=auto_fit.run(self.db,self.config)
        self.assertEqual(first['considered'],1);self.assertEqual(first['queued'],0)
        self.assertEqual(auto_fit.run(self.db,self.config)['considered'],0)
        import copy
        updated=copy.deepcopy(self.cand);updated['fit']['max_jd_age_hours']+=1
        with patch.object(candidate,'load',return_value=updated):
            changed=auto_fit.run(self.db,self.config)
        self.assertEqual(changed['considered'],1);self.assertEqual(changed['queued'],0)

    def packet(self,jd,title='Backend Java Developer'):
        row={'jd_text':jd,'title':title}
        return auto_fit.build_packet(row,self.cand,self.config)
    def test_clear_backend_role_is_high_with_project_sql_and_unverified_fundamentals_noted(self):
        packet,reasons,notes=self.packet(NEWGEN)
        self.assertEqual(reasons,[])
        self.assertIn('multithreading',' '.join(notes['unverified_fundamentals']))
        names={item['name']:item['basis'] for item in packet['requirements']}
        self.assertEqual(names['mysql_sql'],'project');self.assertEqual(names['java'],'work')
    def test_tag_cloud_kafka_is_ignored_but_required_kafka_blocks(self):
        job={'jd_text':NEWGEN,'availability':'Live','location':'Bengaluru','checked_at':job_db.now()}
        packet,_,_=self.packet(NEWGEN)
        import fit_engine
        self.assertTrue(fit_engine.evaluate(job,packet,self.config,ROOT/'master-resume/Yugal_Teotia_CV.tex')['eligible'])
        blocked=NEWGEN.replace('Job Requirements :\n','Job Requirements :\n\nMust have hands-on Kafka experience.\n')
        job['jd_text']=blocked;packet,_,_=self.packet(blocked)
        verdict=fit_engine.evaluate(job,packet,self.config,ROOT/'master-resume/Yugal_Teotia_CV.tex')
        self.assertFalse(verdict['eligible']);self.assertFalse(verdict['strong_medium_eligible'])
    def test_preferred_gaps_do_not_block_and_alternatives_need_one_supported_member(self):
        jd=NEWGEN.replace('Role: Software','Preferred Skills\nKafka, Kubernetes, AWS\nRole: Software')
        packet,reasons,notes=self.packet(jd)
        self.assertEqual(reasons,[]);self.assertIn('messaging',notes['other_gaps'])
        alt=NEWGEN.replace('such as MySQL or similar technologies','such as Oracle, PostgreSQL or MySQL')
        packet,_,_=self.packet(alt)
        self.assertNotIn('other_databases',[i['name'] for i in packet['requirements']])
    def test_stack_line_gap_blocks_even_under_responsibilities(self):
        jd=NEWGEN.replace('using Spring Boot.','using Spring Boot and Oracle.')
        packet,_,_=self.packet(jd)
        self.assertIn('other_databases',[i['name'] for i in packet['requirements']])
    def test_seniority_years_and_junior_bands_are_rejected(self):
        self.assertIsNotNone(self.packet(NEWGEN,'Senior Java Developer')[1])
        self.assertFalse(self.packet(NEWGEN,'Senior Java Developer')[1])
        self.assertTrue(self.packet(NEWGEN.replace('2 - 4 years','5 - 8 years').replace('2-4 years','5+ years'))[1])
        self.assertTrue(self.packet(NEWGEN.replace('2 - 4 years','0 - 1 years').replace('2-4 years','0-1 years'))[1])
    def test_run_binds_portal_id_queues_and_is_idempotent(self):
        job_id=self.job(NEWGEN)
        summary=auto_fit.run(self.db,self.config)
        self.assertEqual(summary['queued'],1,summary)
        row=self.db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        self.assertEqual((row['state'],row['fit'],row['requisition_id']),('Shortlisted','High','160926009207'))
        self.assertEqual(self.db.execute('SELECT state FROM queue WHERE job_id=?',(job_id,)).fetchone()['state'],'QueuedResume')
        self.assertEqual(auto_fit.run(self.db,self.config)['considered'],0)
    def test_uncertain_duplicate_is_never_queued(self):
        job_id=self.job(NEWGEN)
        provenance=json.loads(self.db.execute('SELECT provenance_json FROM jobs WHERE id=?',(job_id,)).fetchone()[0])
        provenance['duplicate_check']['status']='uncertain'
        self.db.execute('UPDATE jobs SET provenance_json=? WHERE id=?',(json.dumps(provenance),job_id))
        summary=auto_fit.run(self.db,self.config)
        self.assertEqual((summary['queued'],summary['blocked']),(0,1))

class AutoResumeAndWorkerTests(Fixture):
    def test_resume_is_verbatim_master_visually_clean_and_worker_binding_accepts_it(self):
        job_id=self.job(NEWGEN);auto_fit.run(self.db,self.config)
        with patch.object(auto_resume,'ROOT',self.tmp),patch.object(job_db,'ROOT',self.tmp):
            result=auto_resume.run(self.db,self.config)
            self.assertEqual(result['prepared'],1,result)
            job=self.db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
            self.assertEqual(job['state'],'ResumeReady')
            folder=self.tmp/'jobs'/job['slug'];spec=json.loads((folder/'resume_spec.json').read_text())
            self.assertEqual(auto_resume.grounded(spec),[])
            self.assertIn('Sep 2026',spec['experience'][0]['dates'])
            review=json.loads((folder/'resume_visual_review.json').read_text())
            self.assertEqual(review['status'],'visually_clean');self.assertEqual(review['pages'],1)
            self.assertTrue(review['pdf'].startswith('Yugal_Teotia_') and review['pdf'].endswith('.pdf'))
            evidence=self.db.execute("SELECT path FROM evidence WHERE job_id=? AND kind='resume_review'",(job_id,)).fetchone()
            with patch.object(worker,'ROOT',self.tmp):
                binding=worker.validate_binding(dict(job),evidence['path'],review,'CONFIRMED profile',self.config)
            self.assertEqual(binding['resume_filename'],review['pdf'])
            self.assertEqual([r['id'] for r in worker.candidates(self.db,self.config)],[job_id])
    def test_unapproved_template_produces_needs_review_not_worker_ready(self):
        job_id=self.job(NEWGEN);auto_fit.run(self.db,self.config)
        with patch.object(auto_resume,'ROOT',self.tmp),patch.object(job_db,'ROOT',self.tmp),\
             patch.object(auto_resume,'template_approved',return_value=False):
            result=auto_resume.run(self.db,self.config)
        self.assertEqual(result['failed'],1)
        self.assertEqual(self.db.execute('SELECT state FROM queue WHERE job_id=?',(job_id,)).fetchone()['state'],'ResumeFailed')
        self.assertEqual(self.db.execute('SELECT state FROM jobs WHERE id=?',(job_id,)).fetchone()['state'],'Shortlisted')
    def test_fabricated_bullet_is_detected(self):
        spec=auto_resume.build_spec({'title':'Backend Java Developer','jd_text':NEWGEN})
        spec['experience'][0]['bullets'][0]='Led a team of 10 engineers'
        self.assertEqual(auto_resume.grounded(spec),['Led a team of 10 engineers'])
    def test_repeated_pre_claim_blocks_back_off_then_hand_off_to_user(self):
        job_id=self.job(NEWGEN);auto_fit.run(self.db,self.config)
        self.db.execute("UPDATE jobs SET state='ResumeReady' WHERE id=?",(job_id,))
        self.db.execute("UPDATE queue SET state='ResumeReady',attempts=1 WHERE job_id=?",(job_id,))
        row=self.db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        worker._note_block(self.db,row)
        self.assertEqual(worker.candidates(self.db,self.config),[])
        worker._note_block(self.db,row)
        self.assertEqual(self.db.execute('SELECT state FROM jobs WHERE id=?',(job_id,)).fetchone()['state'],'NeedsAnswer')
    def test_high_fit_precedes_strong_medium_and_mail_history_gap_blocks_employer(self):
        first=self.job(NEWGEN);second=self.job(NEWGEN.replace('Bengaluru','Pune'),portal='170926009999',employer='Other Co')
        for job_id,fit in ((first,'Medium'),(second,'High')):
            self.db.execute("UPDATE jobs SET state='ResumeReady',fit=? WHERE id=?",(fit,job_id))
            self.db.execute("INSERT OR REPLACE INTO queue(job_id,priority,queued_at,state) VALUES(?,?,?,'ResumeReady')",(job_id,150 if fit=='Medium' else 100,job_db.now()))
        self.assertEqual([r['id'] for r in worker.candidates(self.db,self.config)][0],second)
        folder=self.tmp/'.workflow/reconcile';folder.mkdir(parents=True)
        gap={'schema':2,'message_id':'m','employer_key':job_db.employer('Other Co'),'role':'Backend Java Developer','requisition_id':None,'scope':'role'}
        (folder/'history-gaps.json').write_text(json.dumps([gap]))
        with patch.object(response_reconcile,'ROOT',self.tmp):
            self.assertEqual([r['id'] for r in worker.candidates(self.db,self.config)],[first])   # same employer + same role: blocked

class RestoreAndHistoryTests(unittest.TestCase):
    def test_generic_resume_restore_runs_inside_live_browser_and_after_guard_removal(self):
        events=[]
        class Context:
            def unroute_all(self,behavior=None):events.append('unroute_guard')
        @contextlib.contextmanager
        def fake_chrome(headless=False):
            try:yield Context()
            finally:events.append('browser_closed')
        def restore(page,pdf):
            events.append('restore');return {'after':pdf.name}
        state={'page':object(),'restore_needed':True,'restored':None}
        with patch.object(worker,'_chrome',fake_chrome),patch.object(worker,'upload_profile',restore),\
             patch.object(worker,'current_filename',lambda page:worker.GENERIC.name):
            with self.assertRaises(RuntimeError):
                with worker._browser_session(state):raise RuntimeError('mid-application failure')
        self.assertEqual(events,['unroute_guard','restore','browser_closed']);self.assertTrue(state['restored'])
        events.clear();state.update(restore_needed=False,restored=None)
        with patch.object(worker,'_chrome',fake_chrome):
            with worker._browser_session(state):pass
        self.assertEqual(events,['browser_closed']);self.assertIsNone(state['restored'])
    def test_failed_restore_is_reported_not_hidden(self):
        @contextlib.contextmanager
        def fake_chrome(headless=False):
            yield type('C',(),{'unroute_all':lambda self,behavior=None:None})()
        def broken(page,pdf):raise RuntimeError('upload failed')
        state={'page':object(),'restore_needed':True,'restored':None}
        with patch.object(worker,'_chrome',fake_chrome),patch.object(worker,'upload_profile',broken):
            with worker._browser_session(state):pass
        self.assertIs(state['restored'],False)
    def test_history_sweep_scrolls_the_list_container_before_clicking_load_more(self):
        class Fake:
            rows=10;total=94;scrolled=False
            def __init__(self):self.clicks=0
            def goto(self,*a,**k):pass
            def wait_for_timeout(self,ms):pass
            def locator(self,selector):
                page=self
                class L:
                    def filter(self,**k):return self
                    def count(self):return page.rows if selector=='.jdTuplesContainer .jdTupleContainer' else 1
                    def is_visible(self):return page.rows<page.total
                    def click(self,**k):
                        if selector=='#loadMoreLnk':
                            if not page.scrolled:raise RuntimeError('Element is not visible')
                            page.rows=min(page.total,page.rows+20);page.scrolled=False;page.clicks+=1
                return L()
            def evaluate(self,script,arg=None):
                if 'scrollTop' in script:self.scrolled=True;return None
                return {'rows':self.rows,'total':self.total,'exact':0,'company_role':0,'more':self.rows<self.total}
        page=Fake()
        result=worker._history(page,'160926009207','Newgen','Backend Java Developer')
        self.assertTrue(result['full']);self.assertEqual(page.rows,94)

class ReconcileAndStatusTests(Fixture):
    def test_matches_are_labelled_and_read_only(self):
        job_id=self.job(NEWGEN,employer='Newgen')
        self.db.execute("UPDATE jobs SET requisition_id='160926009207',state='Applied' WHERE id=?",(job_id,))
        report=response_reconcile.reconcile(self.db,[
            {'message_id':'1','subject':'Interview invitation 160926009207 at Newgen','company':'Newgen'},
            {'message_id':'2','subject':'Your application to Backend Java Developer at Newgen','company':'Newgen'},
            {'message_id':'3','subject':'Your application to Java Engineer at Nobody Corp','company':'Nobody Corp'},
            {'message_id':'4','subject':'Jobs for you: Java','company':'Newgen'}])
        kinds=[(i['kind'],i['confidence']) for i in report['items']]
        self.assertEqual(kinds,[('interview','exact'),('acknowledgment','probable'),('acknowledgment','unmatched'),('alert','ignored')])
        self.assertEqual(len(report['urgent']),1)
        self.assertTrue(any('history_gap' in l for l in report['items'][2]['labels']))
        self.assertEqual(self.db.execute('SELECT state FROM jobs WHERE id=?',(job_id,)).fetchone()[0],'Applied')
    def test_status_banner_renders_on_empty_and_populated_state(self):
        html=quick_status.render(quick_status.collect(self.db,self.tmp,self.config))
        self.assertIn('Naukri auto-apply',html);self.assertIn('Mailbox reconciliation',html)
        self.job(NEWGEN);auto_fit.run(self.db,self.config)
        self.assertIn('Backend Java Developer',quick_status.render(quick_status.collect(self.db,self.tmp,self.config)))

class MasterCheckTests(unittest.TestCase):
    def test_check_reports_structured_result(self):
        result=master_check.check()
        self.assertIn('ok',result);self.assertIn('issues',result)

if __name__=='__main__':unittest.main()

class ApplyNowTests(unittest.TestCase):
    def run_cli(self,argv):
        import io,contextlib as cl
        import workflow,pipeline_v2
        calls=[]
        def fake(**kwargs):
            calls.append(kwargs);return {'status':'Success','sources':{},'stage_counts':{},'test':kwargs.get('test'),'evidence_paths':[]}
        out=io.StringIO()
        with patch.object(pipeline_v2,'run',fake),patch.object(sys,'argv',['workflow']+argv),cl.redirect_stdout(out),\
             patch('core_run.ensure_service_path'),patch('sys.stderr',io.StringIO()):
            try:workflow.main()
            except SystemExit:pass
        return calls[0]
    def test_apply_now_and_scheduled_service_share_one_pipeline_call(self):
        scheduled=self.run_cli(['daily','--scheduled','--max-seconds','1800','--sources','naukri,linkedin','--page-cap','1','--query-cap','2'])
        live=self.run_cli(['apply-now'])
        dry=self.run_cli(['apply-now','--dry-run'])
        for key in ('scheduled','max_seconds','page_cap','query_cap','sources','review_limit','fresh_only','test'):
            self.assertEqual(live.get(key) or None,scheduled.get(key) or None,key)
        self.assertTrue(live['scheduled']);self.assertFalse(live['test'])
        self.assertTrue(dry['scheduled']);self.assertTrue(dry['test'])
        self.assertTrue(callable(live['progress']))
    def test_refuses_when_another_run_is_active(self):
        import fcntl,pipeline_v2,apply_now
        pipeline_v2.STATE.mkdir(exist_ok=True)
        with (pipeline_v2.STATE/'daily-run.lock').open('a') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            result=pipeline_v2.run(test=True,scheduled=True)
        self.assertEqual(result['status'],'OverlapPrevented')
        text,started=apply_now.summarize(result)
        self.assertFalse(started);self.assertIn('another run is active',text)
    def test_summary_lists_confirmed_blockers_and_evidence(self):
        import apply_now
        result={'status':'Partial','sources':{'naukri':{'status':'Success'},'foundit':{'status':'NoVisibleCards'}},
                'blockers':[{'source':'foundit','reason':'NoVisibleCards'}],'stage_counts':{'original_or_portal_jds_reviewed':3},
                'auto_fit':{'considered':2,'queued':1,'not_eligible':1,'blocked':0},'errors':[],'blocker_alerts':[{}],
                'naukri_application_worker':{'status':'Complete','candidate_count':1,'results':[
                    {'job_id':250,'status':'Uncertain','pending_question':'x','generic_resume_restored':True}]},
                'evidence_paths':['.workflow/runs/x.json']}
        text,started=apply_now.summarize(result)
        self.assertTrue(started)
        for expected in ('Sources checked: 2 (1 OK)','foundit: NoVisibleCards','Applications confirmed: 0','Blocker: source foundit','[Uncertain]','.workflow/runs/x.json'):
            self.assertIn(expected,text)
