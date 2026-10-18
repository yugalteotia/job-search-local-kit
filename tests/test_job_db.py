import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import job_config
import job_db
import job_views

REPO=Path(__file__).resolve().parents[1]

class ConfigAndMigrationTests(unittest.TestCase):
    def test_config_rejects_unlisted_host_and_weakened_fit(self):
        config=job_config.load()
        bad=copy.deepcopy(config);bad['sources']['indeed']['search_url']='https://other.example/jobs?q={query}&l={location}'
        with self.assertRaises(ValueError):job_config.validate(bad)
        bad=copy.deepcopy(config);bad['high_fit']['minimum_essential_coverage']=.8
        with self.assertRaises(ValueError):job_config.validate(bad)
        bad=copy.deepcopy(config);bad['submissions']['enabled']=True
        with self.assertRaises(ValueError):job_config.validate(bad)
        bad=copy.deepcopy(config);bad['direct_requisition_rules']['lever']['hosts']=['jobs.unknown.example']
        with self.assertRaises(ValueError):job_config.validate(bad)
    def test_migration_is_lossless_and_views_match(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'.workflow/cleanup').mkdir(parents=True)
            shutil.copy2(REPO/'tracker.xlsx',root/'tracker.xlsx')
            shutil.copy2(REPO/'.workflow/cleanup/other_history_keys.json',root/'.workflow/cleanup/other_history_keys.json')
            result=job_db.migrate(root)
            self.assertEqual(result['legacy_rows'],result['sqlite_rows'])
            self.assertEqual(result['optum_2388999']['state'],'Rejected')
            self.assertEqual(result['optum_2388999']['known_reason'],'reason unknown')
            self.assertGreaterEqual(result['dedup_keys'],4)
            self.assertEqual(job_views.regenerate(root,root/'.workflow/jobs.sqlite3')['tracker_rows'],result['legacy_rows'])

class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'jobs/acme-java-a1').mkdir(parents=True)
        (self.root/'master-resume').mkdir()
        shutil.copy2(REPO/'master-resume/Yugal_Teotia_CV.tex',self.root/'master-resume/Yugal_Teotia_CV.tex')
        self.old=(job_db.ROOT,job_db.STATE)
        job_db.ROOT=self.root;job_db.STATE=self.root/'.workflow'
        self.db=job_db.connect(self.root/'.workflow/jobs.sqlite3');job_db.initialize(self.db)
        self.job,_=job_db.create_job(self.db,{'employer':'Acme','title':'Java Engineer','requisition_id':'A-1',
          'canonical_url':'https://ats.example/jobs/A-1','source':'test','location':'Pune, India','discovered_at':job_db.now(),'slug':'acme-java-a1'})
    def tearDown(self):
        self.db.close();job_db.ROOT,job_db.STATE=self.old;self.temp.cleanup()
    def test_verified_portal_id_is_bound_once_and_rejects_history(self):
        url='https://www.naukri.com/job-listings-java-backend-example-190826038395'
        record={'employer':'Infosys','title':'Java Backend Developer','canonical_url':url,
                'source':'naukri_portal_native','location':'Pune, India','discovered_at':job_db.now(),
                'provenance':{'verification_method':'portal_native','portal_job_id':'190826038395',
                              'duplicate_check':{'status':'clear'}}}
        portal,_=job_db.create_job(self.db,record)
        job_db.transition(self.db,portal,'Verified','test','exact portal JD')
        self.assertEqual(job_db.bind_verified_portal_id(self.db,portal),'190826038395')
        self.assertEqual(job_db.bind_verified_portal_id(self.db,portal),'190826038395')
        self.assertEqual(self.db.execute('SELECT requisition_key FROM jobs WHERE id=?',(portal,)).fetchone()[0],
                         'req:190826038395')
        second_url='https://www.naukri.com/job-listings-java-backend-example-290926038395'
        second,_=job_db.create_job(self.db,{**record,'canonical_url':second_url,'slug':'infosys-java-second',
            'provenance':{**record['provenance'],'portal_job_id':'290926038395'}})
        job_db.transition(self.db,second,'Verified','test','exact portal JD')
        self.db.execute("INSERT INTO history_keys VALUES(?,?,?,?,?,?,?,?,?,?)",
            ('infosys','req:290926038395','Infosys','Java Backend Developer','290926038395',
             second_url,'Rejected',job_db.now(),'reason unknown','test'))
        with self.assertRaisesRegex(ValueError,'terminal history'):
            job_db.bind_verified_portal_id(self.db,second)
    def test_infosys_manual_pilot_is_exactly_scoped_and_test_mode_blocked(self):
        config=job_config.load()
        job={'id':job_db.INFOSYS_PILOT_JOB_ID,
             'requisition_id':job_db.INFOSYS_PILOT_REQUISITION,
             'source':'naukri_portal_native','fit':'Medium','state':'Prepared'}
        url=job_db.INFOSYS_PILOT_FORM_URL
        self.assertTrue(job_db._scoped_pilot_allowed(job,url,config,False))
        self.assertFalse(job_db._scoped_pilot_allowed(job,url,config,True))
        for changed in ({'id':999},{'requisition_id':'different'},{'state':'NeedsAnswer'},
                        {'fit':'Low'},{'source':'linkedin_portal_native'}):
            self.assertFalse(job_db._scoped_pilot_allowed({**job,**changed},url,config,False))
        self.assertFalse(job_db._scoped_pilot_allowed(job,url+'?other=1',config,False))
    def test_retired_one_off_pilot_no_longer_bypasses_worker_gates(self):
        config=job_config.load()
        self.assertEqual(config['mode'],'read_only')
        self.assertFalse(config['submissions']['enabled'])
        job={'id':244,
             'requisition_id':'070926003879',
             'source':'naukri_portal_native','fit':'Medium','state':'Prepared'}
        url='https://www.naukri.com/job-listings-java-backend-developer-naukri-e-hire-campaign-mumbai-1-to-4-years-070926003879'
        self.assertFalse(job_db._scoped_pilot_allowed(job,url,config,True))
        self.assertFalse(job_db._scoped_pilot_allowed(job,url,config,False))
    def test_infosys_native_save_requires_recent_profile_binding_and_unique_claim(self):
        folder=self.root/'jobs/naukri-190826038395';folder.mkdir()
        url=job_db.INFOSYS_PILOT_FORM_URL
        stamp=job_db.now()
        self.db.execute('''INSERT INTO jobs(id,employer_key,requisition_key,employer,title,requisition_id,
            canonical_url,slug,source,discovered_at,fit,state,created_at,updated_at)
            VALUES(220,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            ('infosys','req:190826038395','Infosys','Java Backend Developer',
             job_db.INFOSYS_PILOT_REQUISITION,url,'naukri-190826038395',
             'naukri_portal_native',stamp,'Medium','Prepared',stamp,stamp))
        resume=folder/'Yugal_Teotia_Java_Backend_Developer.pdf';resume.write_bytes(b'%PDF-unit-test')
        form=folder/'native-presubmit.json'
        checks={key:True for key in job_db.INFOSYS_PILOT_CHECKS}
        packet={'job_id':220,'portal_job_id':job_db.INFOSYS_PILOT_REQUISITION,
            'form_url':url,'checks':checks,'provider':'naukri_native_chat',
            'profile_resume_filename':resume.name,'profile_resume_sha256':hashlib.sha256(resume.read_bytes()).hexdigest(),
            'screening_question':'Are you available for a interview?(Virtual)','screening_answer_confirmed':True,
            'potential_submit_action':'screening_chat_save','signed_in':True,'account_name_matches':False,
            'exact_listing_id':job_db.INFOSYS_PILOT_REQUISITION,
            'profile_verified_at':job_db.now(),'listing_checked_at':job_db.now()}
        form.write_text(json.dumps(packet))
        rr=str(resume.relative_to(self.root));fr=str(form.relative_to(self.root))
        rh=job_db.record_evidence(self.db,220,'resume_review',rr)
        fh=job_db.record_evidence(self.db,220,'form_review',fr)
        with self.assertRaises(ValueError):
            job_db.begin_attempt(self.db,220,rr,rh,url,fr,fh,pilot=True,test_mode=True)
        with self.assertRaisesRegex(ValueError,'signed-in account'):
            job_db.begin_attempt(self.db,220,rr,rh,url,fr,fh,pilot=True)
        packet['account_name_matches']=True
        form.write_text(json.dumps(packet));fh=job_db.record_evidence(self.db,220,'form_review',fr)
        attempt=job_db.begin_attempt(self.db,220,rr,rh,url,fr,fh,pilot=True)
        self.assertTrue(attempt)
        with self.assertRaises(ValueError):
            job_db.begin_attempt(self.db,220,rr,rh,url,fr,fh,pilot=True)
    def test_unknown_chat_outcome_can_be_locked_without_claiming_applied(self):
        job_db.transition(self.db,self.job,'Verified','test','JD')
        job_db.transition(self.db,self.job,'Shortlisted','test','fit')
        job_db.transition(self.db,self.job,'NeedsAnswer','test','screening answer')
        job_db.transition(self.db,self.job,'NeedsReview','test','chat completion without application proof')
        self.assertEqual(self.db.execute('SELECT state FROM jobs WHERE id=?',(self.job,)).fetchone()[0],'NeedsReview')
        self.assertEqual(self.db.execute('SELECT count(*) FROM submission_attempts WHERE job_id=?',(self.job,)).fetchone()[0],0)
        with self.assertRaises(ValueError):
            job_db.transition(self.db,self.job,'Applied','test','no confirmation')

    def test_resolved_answer_can_resume_in_progress_without_creating_attempt(self):
        job_db.transition(self.db,self.job,'Verified','test','JD')
        job_db.transition(self.db,self.job,'InProgress','test','questionnaire opened')
        job_db.transition(self.db,self.job,'NeedsAnswer','test','required answer requested')
        job_db.transition(self.db,self.job,'InProgress','test','confirmed answer available; no save sent')
        self.assertEqual(self.db.execute('SELECT state FROM jobs WHERE id=?',(self.job,)).fetchone()[0],'InProgress')
        self.assertEqual(self.db.execute('SELECT count(*) FROM submission_attempts WHERE job_id=?',(self.job,)).fetchone()[0],0)
    def test_transition_proof_and_terminal_are_enforced(self):
        with self.assertRaises(ValueError):job_db.transition(self.db,self.job,'Applied','test','skip')
        with self.assertRaises(ValueError):job_db.transition(self.db,self.job,'Rejected','test','invent reason')
        job_db.transition(self.db,self.job,'Verified','test','original page')
        job_db.transition(self.db,self.job,'Shortlisted','test','fit')
        with self.assertRaises(ValueError):job_db.transition(self.db,self.job,'Rejected','test','no application')
        second,created=job_db.create_job(self.db,{'employer':'Acme','title':'Java Engineer','requisition_id':'A-1',
          'canonical_url':'https://ats.example/jobs/A-1','source':'test','discovered_at':job_db.now()})
        self.assertEqual(second,self.job);self.assertFalse(created)
    def test_uncertain_portal_native_duplicate_blocks_high_fit_queue(self):
        jd='Java backend APIs using Spring Boot in Pune, India. 1-2 years of experience. Build REST APIs and Java services.'
        portal,_=job_db.create_job(self.db,{'employer':'Acme','title':'Java Engineer','requisition_id':None,
          'canonical_url':'https://www.linkedin.com/jobs/view/4471320992','source':'linkedin_portal_native',
          'location':'Pune, India','discovered_at':job_db.now(),'checked_at':job_db.now(),'availability':'Live',
          'jd_text':jd,'provenance':{'verification_method':'portal_native','portal':'linkedin','portal_job_id':'4471320992',
                                     'duplicate_check':{'status':'uncertain'}}})
        job_db.transition(self.db,portal,'Verified','portal_test','visible portal-native JD')
        verdict={'fit':'High','eligible':True,'reasons':[],'jd_sha256':'x','essential_total':3,'essential_work_supported':3}
        with patch('fit_engine.evaluate',return_value=verdict):
            actual=job_db.apply_fit_decision(self.db,portal,{},job_config.load())
        self.assertEqual(actual['fit'],'High')
        self.assertFalse(actual['eligible'])
        self.assertTrue(actual['submission_blocked_by_duplicate'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM queue WHERE job_id=?',(portal,)).fetchone()[0],0)
    def test_latest_title_only_page_blocks_stale_fit_packet(self):
        job_db.transition(self.db,self.job,'Verified','test','original page')
        self.db.execute('UPDATE jobs SET provenance_json=? WHERE id=?',
                        (json.dumps({'latest_live_jd_status':'TitleOnly'}),self.job))
        with self.assertRaisesRegex(ValueError,'Latest exact job page lacks a substantive JD'):
            job_db.apply_fit_decision(self.db,self.job,{},job_config.load())
    def test_verified_original_refresh_persists_posting_date_and_location(self):
        record={'employer':'Acme','title':'Java Engineer Updated','requisition_id':'A-1',
          'canonical_url':'https://ats.example/jobs/A-1','source':'test','location':'Pune, India',
          'discovered_at':job_db.now(),'posted_at':'2026-09-20','checked_at':job_db.now(),'jd_text':'Duties and qualifications text.',
          'availability':'Live','provenance':{'evidence':'exact page'}}
        job_id,created=job_db.create_job(self.db,record)
        self.assertFalse(created)
        row=self.db.execute('SELECT title,location,posted_at,jd_text,provenance_json FROM jobs WHERE id=?',(job_id,)).fetchone()
        self.assertEqual(row['title'],'Java Engineer Updated')
        self.assertEqual(row['location'],'Pune, India')
        self.assertEqual(row['posted_at'],'2026-09-20')
        self.assertEqual(row['jd_text'],'Duties and qualifications text.')
        self.assertIn('exact page',row['provenance_json'])
    def test_test_mode_and_exact_attempt_binding(self):
        for state in ('Verified','Shortlisted','ResumeReady','Preparing','Prepared'):
            job_db.transition(self.db,self.job,state,'test','prepared')
        jd='Java backend APIs using Spring Boot in Pune, India. 1-2 years of experience. Build REST APIs and Java services.'
        decision={'jd_sha256':hashlib.sha256(jd.encode()).hexdigest(),
          'java_excerpt':'Java backend APIs','spring_boot_excerpt':'Spring Boot in Pune',
          'backend_excerpt':'backend APIs using Spring Boot','location_excerpt':'Pune, India. 1-2 years',
          'experience_excerpt':'1-2 years of experience','required_professional_years':1,
          'requirements':[{'name':'Java','essential':True,'jd_excerpt':'Java backend APIs','basis':'work','master_work_excerpt':'20 Java modules'}]}
        self.db.execute("UPDATE jobs SET fit='High',jd_text=?,availability='Live',checked_at=?,fit_json=? WHERE id=?",
                        (jd,job_db.now(),json.dumps({'decision':decision}),self.job))
        folder=self.root/'jobs/acme-java-a1';resume=folder/'Yugal_Teotia_Java_Engineer.pdf';resume.write_bytes(b'resume')
        form=folder/'form-review.json';form.write_text('{"checked":true}')
        resume_rel=str(resume.relative_to(self.root));form_rel=str(form.relative_to(self.root))
        resume_hash=job_db.record_evidence(self.db,self.job,'resume_review',resume_rel)
        form_hash=job_db.record_evidence(self.db,self.job,'form_review',form_rel)
        prod=copy.deepcopy(job_config.load());prod['mode']='production';prod['submissions']['enabled']=True
        (self.root/'.workflow/full-application-path-verified.json').write_text('{}')
        with self.assertRaises(ValueError):job_db.begin_attempt(self.db,self.job,resume_rel,resume_hash,'https://ats.example/jobs/A-1',form_rel,form_hash,test_mode=True,config=prod)
        with self.assertRaises(ValueError):job_db.begin_attempt(self.db,self.job,form_rel,resume_hash,'https://ats.example/jobs/A-1',form_rel,form_hash,config=prod)
        attempt=job_db.begin_attempt(self.db,self.job,resume_rel,resume_hash,'https://ats.example/jobs/A-1',form_rel,form_hash,config=prod)
        with self.assertRaises(ValueError):job_db.begin_attempt(self.db,self.job,resume_rel,resume_hash,'https://ats.example/jobs/A-1',form_rel,form_hash,config=prod)
        with self.assertRaises(ValueError):job_db.transition(self.db,self.job,'Applied','test','no proof','fake')
        captured=job_db.now();proof=folder/'confirmation.json';proof.write_text(json.dumps({'attempt_id':attempt,'requisition_id':'A-1','captured_at':captured,'url':'https://ats.example/confirmed','confirmation_text':'Application received'}))
        self.assertEqual(job_db.confirm_attempt(self.db,attempt,self.job,str(proof.relative_to(self.root)),captured,config=prod),'Applied')
        with self.assertRaises(ValueError):job_db.transition(self.db,self.job,'Rejected','test','unknown reason')
        response=folder/'rejection.json';response.write_text('{"portal_status":"Rejected"}')
        response_rel=str(response.relative_to(self.root));job_db.record_evidence(self.db,self.job,'response',response_rel)
        self.assertEqual(job_db.transition(self.db,self.job,'Rejected','test','portal status',response_rel),'Rejected')
        with self.assertRaises(ValueError):job_db.transition(self.db,self.job,'Shortlisted','test','retry')

if __name__=='__main__':unittest.main()
