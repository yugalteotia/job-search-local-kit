import sys
from pathlib import Path
import datetime as dt
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import lead_resolution as l
import job_config
import job_db

class LeadTests(unittest.TestCase):
    def test_navigation_is_not_a_job(self):
        self.assertEqual(l.classify('naukri','https://www.naukri.com/faq/job-seeker'),'NotAJob')
        self.assertEqual(l.classify('cutshort','https://cutshort.io/jobs/java-jobs-in-pune'),'NotAJob')
        self.assertEqual(l.classify('indeed','https://in.indeed.com/viewjob'),'NotAJob')
    def test_exact_job_identifiers_survive(self):
        self.assertEqual(l.classify('linkedin','https://www.linkedin.com/jobs/view/4471312994/'),'NeedsOriginal')
        self.assertEqual(l.classify('indeed','https://in.indeed.com/viewjob?jk=1234567890abcdef'),'NeedsOriginal')
        self.assertEqual(l.classify('foundit','https://www.foundit.in/job/java-developer-company-pune-68178832'),'NeedsOriginal')
        self.assertEqual(l.classify('foundit','https://jda.wd5.myworkdayjobs.com/en-US/JDA_Careers/job/Hyderabad/Sr-Software-Engineer_262141'),'NeedsOriginal')
        self.assertEqual(l.classify('foundit','https://www.linkedin.com/jobs/view/4461778247/'),'NeedsOriginal')
        self.assertEqual(l.classify('foundit','https://www.foundit.in/search/java-jobs'),'NotAJob')
        self.assertEqual(l.classify('foundit','https://unrelated.example/job/java-engineer_262141'),'NotAJob')
    def test_ranked_selection_prefers_backend_java_and_interleaves_sources(self):
        rows=[
            {'source':'linkedin','title':'Principal Software Engineer','last_seen':'2026-09-28T11:00:00+00:00'},
            {'source':'linkedin','title':'Java Backend Engineer','last_seen':'2026-09-28T10:00:00+00:00'},
            {'source':'indeed','title':'Manager | Fullstack Java','last_seen':'2026-09-28T11:00:00+00:00'},
        ]
        selected=l.select_pending(rows,3)
        self.assertEqual(selected[0]['title'],'Java Backend Engineer')
        self.assertEqual(selected[1]['source'],'indeed')
    def test_direct_provider_rules_allow_new_employers_but_reject_unrelated_urls(self):
        cfg=job_config.load()['direct_requisition_rules']
        self.assertEqual(l.provider_for_url('https://jobs.lever.co/newemployer/12345678-1234-1234-1234-123456789012',cfg),'lever')
        self.assertEqual(l.provider_for_url('https://job-boards.greenhouse.io/newco/jobs/123456',cfg),'greenhouse')
        self.assertIsNone(l.provider_for_url('https://jobs.lever.co/newemployer',cfg))
        self.assertIsNone(l.provider_for_url('https://random.example/jobs/123456',cfg))
        rule=cfg['employer_site']
        self.assertEqual(l.provider_for_url('https://careers.newco.example/jobs/java-backend-123',cfg,'Apply now','www.linkedin.com'),'employer_site')
        self.assertIsNone(l.provider_for_url('https://random.example/blog/technology',cfg,'Apply now','www.linkedin.com'))
        self.assertIsNone(l.provider_for_url('https://random.example/jobs/123',cfg,'Read more','www.linkedin.com'))
    def test_same_job_alert_location_excludes_recommended_jobs_and_bare_remote(self):
        detail={'jd_excerpt':'About the job\nLocation: Remote\n\nSet alert for similar jobs\n\nSoftware Engineer, Mumbai, Maharashtra, India\n\nOff\nMore jobs\nOther Role\nUnited States'}
        self.assertEqual(l.visible_portal_location('Software Engineer',detail),
                         ('Mumbai, Maharashtra, India','same_job_alert_exact_title'))
        detail['jd_excerpt']='About the job\nLocation: Remote\n\nSet alert for similar jobs\n\nOther Role, India\n\nOff\nMore jobs\nSoftware Engineer\nPune'
        self.assertEqual(l.visible_portal_location('Software Engineer',detail),
                         ('Remote','job_description_location_label_no_country'))
        detail['job_header_excerpt']='Software Engineer\n\nMumbai, Maharashtra, India · Reposted 7 hours ago\nOn-site'
        self.assertEqual(l.visible_portal_location('Software Engineer',detail),
                         ('Mumbai, Maharashtra, India','job_header_exact_title'))
    def test_recheck_refreshes_existing_exact_url_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); old_root,old_state=job_db.ROOT,job_db.STATE
            job_db.ROOT=root;job_db.STATE=root/'.workflow'
            db=job_db.connect(root/'.workflow/jobs.sqlite3');job_db.initialize(db)
            try:
                url='https://jobs.lever.co/acme/12345678-1234-1234-1234-123456789012'
                job_id,_=job_db.create_job(db,{'employer':'Acme','title':'Java Backend Engineer',
                    'requisition_id':'AC-1','canonical_url':url,'source':'acme_lever',
                    'location':'Pune, India','discovered_at':job_db.now(),
                    'jd_text':'Java Spring Boot backend','availability':'Live'})
                verified={'verified':True,'employer':'Acme','title':'Java Backend Engineer',
                    'location':'Pune, India','requisition_id':'AC-1','checked_at':job_db.now(),
                    'posted_at':None,'jd_text':'Qualifications: Java Spring Boot backend engineer. '*8,
                    'evidence':{'http_status':200}}
                with patch.object(l,'verify_direct_requisition',return_value=verified):
                    result=l.recheck_missing_ats_jds(db,job_config.load(),limit=1)
                self.assertEqual(result['results'][0]['job_id'],job_id)
                self.assertTrue(result['results'][0]['updated_from_original'])
                row=db.execute('select id,requisition_key,canonical_url,jd_text from jobs').fetchone()
                self.assertEqual(row['id'],job_id)
                self.assertEqual(row['requisition_key'],'req:ac1')
                self.assertEqual(row['canonical_url'],url)
                self.assertIn('Qualifications:',row['jd_text'])
            finally:
                db.close();job_db.ROOT,job_db.STATE=old_root,old_state

if __name__=='__main__':unittest.main()
