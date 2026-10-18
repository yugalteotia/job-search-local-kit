import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import naukri_native_worker as worker
import fit_engine
import job_config
import job_db
import shutil
from types import SimpleNamespace

def enabled_config():
    # Independent of the operational on/off switch in config/job_search.yaml.
    config=job_config.load();config['naukri_worker']['enabled']=True;return config

class NaukriWorkerIsolationTests(unittest.TestCase):
    def test_applied_history_can_confirm_native_chat_but_show_alone_cannot(self):
        show=SimpleNamespace(events=[{'job_id':'701123456789','reason':'claimed_job_status_refresh',
            'response_status':200,'response_metadata':{'job_id_match':True,'dataCommitted':False}}],
            save_arm_count=1,answer_count=1)
        listing={'exact_path':True,'applied':False}
        history={'full':False,'exact':0}
        self.assertIsNone(worker._job_bound_commit(show,'701123456789',listing,history))
        show.events.append({'job_id':'701123456789','reason':'one_claimed_chat_answer',
            'response_status':200,'response_metadata':{'job_id_match':True}})
        history={'full':True,'exact':1}
        self.assertEqual(worker._job_bound_commit(show,'701123456789',listing,history)['reason'],
                         'positive_job_bound_applied_ui_confirmation')

    def test_scheduled_no_submit_path_reports_would_submit_despite_global_submissions_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old_root=worker.ROOT;worker.ROOT=root
            source_root=Path(__file__).resolve().parents[1]
            (root/'master-resume').mkdir()
            shutil.copy2(source_root/'master-resume/Yugal_Teotia_CV.tex',root/'master-resume/Yugal_Teotia_CV.tex')
            (root/'APPLICATION_PROFILE.md').write_text('Synthetic confirmed answer profile: CONFIRMED.')
            folder=root/'jobs/synthetic-would-submit';folder.mkdir(parents=True)
            resume=folder/'Yugal_Teotia_Backend_801.pdf';resume.write_bytes(b'%PDF-synthetic-801')
            digest=hashlib.sha256(resume.read_bytes()).hexdigest()
            (folder/'resume_visual_review.json').write_text(json.dumps({'status':'visually_clean','pdf_sha256':digest}))
            job={'id':801,'requisition_id':'801123456789','slug':'synthetic-would-submit',
                 'source':'naukri_portal_native','state':'Prepared','availability':'Live','fit':'High',
                 'canonical_url':'https://www.naukri.com/job-listings-java-backend-801123456789',
                 'fit_json':json.dumps({'decision':{'synthetic':True},'result':{}}),
                 'jd_text':'Verified synthetic Java Spring Boot backend duties','employer':'Synthetic Co',
                 'title':'Java Backend Developer'}
            class EvidenceDB:
                class Cursor:
                    def fetchone(self):return {'path':str(resume.relative_to(root)),
                        'sha256':hashlib.sha256(resume.read_bytes()).hexdigest()}
                def execute(self,query,params):
                    self.assertion=(query,params)
                    return self.Cursor()
            db=EvidenceDB();config=enabled_config()
            try:
                with patch.object(worker,'candidates',return_value=[job]), patch.object(worker,'resumable',return_value=[]), patch.object(job_db,'naukri_worker_path_verified',return_value=True), patch.object(fit_engine,'evaluate',return_value={'eligible':True,'strong_medium_eligible':False}):
                    result=worker.run(db,config,test_mode=True,max_submissions=1)
                check=result['checks'][0]
                self.assertEqual(check['status'],'WouldSubmit',check)
                self.assertTrue(check['would_submit'])
                self.assertFalse(check['global_submissions_enabled'])
                self.assertFalse(check['actual_submission'])
                self.assertEqual((result['forms_opened'],result['uploads'],result['submissions']),(0,0,0))
            finally:worker.ROOT=old_root

    def test_confirmed_answer_mapping_keeps_notice_and_joining_date_distinct(self):
        profile=(Path(__file__).resolve().parents[1]/'APPLICATION_PROFILE.md').read_text()
        self.assertEqual(worker._answer('What is your notice period?',profile)[0],'Immediate joiner')
        self.assertEqual(worker._answer('When can you join?',profile)[0],'1 October 2026')
        self.assertEqual(worker._answer('Do you have five years of Kafka?',profile),(None,None))

    def test_two_job_ids_and_resumes_remain_isolated_for_high_and_strong_medium(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old_root=worker.ROOT;worker.ROOT=root
            config=enabled_config()
            answer_profile='All reusable answers below are CONFIRMED.'
            jobs=[];reviews=[]
            try:
                for job_id,req,slug,fit,strong in ((501,'501123456789','synthetic-high','High',False),
                                                    (502,'502123456789','synthetic-medium','Medium',True)):
                    folder=root/'jobs'/slug;folder.mkdir(parents=True)
                    pdf=folder/f'Yugal_Teotia_Java_Backend_Developer_{job_id}.pdf'
                    pdf.write_bytes(f'%PDF-{job_id}'.encode())
                    digest=hashlib.sha256(pdf.read_bytes()).hexdigest()
                    review={'status':'visually_clean','pdf_sha256':digest}
                    (folder/'resume_visual_review.json').write_text(json.dumps(review))
                    job={'id':job_id,'requisition_id':req,'slug':slug,'source':'naukri_portal_native',
                         'state':'Prepared','availability':'Live','fit':fit,
                         'canonical_url':f'https://www.naukri.com/job-listings-java-backend-{req}',
                         'fit_json':json.dumps({'decision':{'test':'bound'},'result':{'strong_medium_eligible':strong}}),
                         'jd_text':f'JD {req}','employer':'Synthetic Co','title':'Java Backend Developer'}
                    jobs.append((job,str(pdf.relative_to(root)),review))
                with patch.object(fit_engine,'evaluate',side_effect=[{'eligible':True,'strong_medium_eligible':False},
                                                                      {'eligible':False,'strong_medium_eligible':True}]):
                    first=worker.validate_binding(jobs[0][0],jobs[0][1],jobs[0][2],answer_profile,config)
                    second=worker.validate_binding(jobs[1][0],jobs[1][1],jobs[1][2],answer_profile,config)
                self.assertEqual(first['job_id'],501);self.assertEqual(first['requisition_id'],'501123456789')
                self.assertEqual(second['job_id'],502);self.assertEqual(second['requisition_id'],'502123456789')
                self.assertNotEqual(first['resume_sha256'],second['resume_sha256'])
                with patch.object(fit_engine,'evaluate',return_value={'eligible':True,'strong_medium_eligible':False}):
                    with self.assertRaisesRegex(ValueError,'inside this job folder'):
                        worker.validate_binding(jobs[0][0],jobs[1][1],jobs[0][2],answer_profile,config)
                self.assertEqual(jobs[0][0]['id'],501);self.assertEqual(jobs[1][0]['id'],502)
            finally:worker.ROOT=old_root

    def test_permanent_exclusions_and_four_plus_medium_are_rejected(self):
        config=enabled_config()
        for req in config['naukri_worker']['excluded_requisition_ids']:
            with self.assertRaisesRegex(ValueError,'Permanently excluded'):
                worker.validate_binding({'requisition_id':req},'',{},'CONFIRMED',config)

    def test_attempt_claims_bind_each_synthetic_job_to_its_own_resume_and_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old_root,old_state=job_db.ROOT,job_db.STATE
            job_db.ROOT=root;job_db.STATE=root/'.workflow'
            (root/'master-resume').mkdir();shutil.copy2(Path(worker.ROOT)/'master-resume/Yugal_Teotia_CV.tex',root/'master-resume/Yugal_Teotia_CV.tex')
            db=job_db.connect(root/'.workflow/jobs.sqlite3');job_db.initialize(db);attempts=[]
            config=enabled_config()
            try:
                for job_id,req,slug,fit,strong in ((601,'601123456789','claim-high','High',False),
                                                    (602,'602123456789','claim-medium','Medium',True)):
                    folder=root/'jobs'/slug;folder.mkdir(parents=True)
                    resume=folder/f'Yugal_Teotia_Backend_{job_id}.pdf';resume.write_bytes(f'%PDF-{job_id}'.encode())
                    form=folder/'presubmit.json';checks={k:True for k in ('live','unapplied','fit','resume_visual','resume_selected','answers_confirmed','unique_apply')}
                    form.write_text(json.dumps({'job_id':job_id,'requisition_id':req,
                        'form_url':f'https://www.naukri.com/job-listings-backend-{req}',
                        'provider':'naukri_native_chat','signed_in':True,'account_name_matches':True,
                        'resume_filename':resume.name,'resume_sha256':hashlib.sha256(resume.read_bytes()).hexdigest(),
                        'final_action':'screening_chat_save','checks':checks}))
                    stamp=job_db.now();fit_json=json.dumps({'decision':{'test':job_id},'result':{'strong_medium_eligible':strong}})
                    db.execute('''INSERT INTO jobs(id,employer_key,requisition_key,employer,title,requisition_id,canonical_url,
                        slug,source,location,discovered_at,checked_at,jd_text,availability,fit,fit_json,state,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (job_id,f'co{job_id}',f'req:{req}',f'Company {job_id}','Java Backend Developer',req,
                         f'https://www.naukri.com/job-listings-backend-{req}',slug,'naukri_portal_native','Pune, India',
                         stamp,stamp,'substantive JD', 'Live',fit,fit_json,'Prepared',stamp,stamp))
                    rr=str(resume.relative_to(root));fr=str(form.relative_to(root))
                    resume_sha=job_db.record_evidence(db,job_id,'resume_review',rr)
                    form_sha=job_db.record_evidence(db,job_id,'form_review',fr)
                    with patch.object(fit_engine,'evaluate',return_value={'eligible':fit=='High','strong_medium_eligible':strong}), \
                         patch.object(job_db,'naukri_worker_path_verified',return_value=True):
                        attempts.append(job_db.begin_attempt(db,job_id,rr,resume_sha,
                            f'https://www.naukri.com/job-listings-backend-{req}',fr,form_sha,
                            config=config,naukri_worker=True))
                rows=db.execute('select job_id,resume_path,form_url,state from submission_attempts order by job_id').fetchall()
                self.assertEqual([r['job_id'] for r in rows],[601,602])
                self.assertIn('601',rows[0]['resume_path']);self.assertIn('602',rows[1]['resume_path'])
                self.assertTrue(rows[0]['form_url'].endswith('601123456789'))
                self.assertTrue(rows[1]['form_url'].endswith('602123456789'))
                self.assertEqual([r['state'] for r in rows],['uncertain','uncertain'])
                with self.assertRaises(ValueError):
                    job_db.begin_attempt(db,601,'jobs/claim-high/Yugal_Teotia_Backend_601.pdf','0'*64,
                        'https://www.naukri.com/job-listings-backend-601123456789','jobs/claim-high/presubmit.json','0'*64,
                        config=config,naukri_worker=True)
            finally:
                db.close();job_db.ROOT,job_db.STATE=old_root,old_state

if __name__=='__main__':unittest.main()
