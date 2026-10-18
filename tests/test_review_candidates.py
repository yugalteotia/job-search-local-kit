import sys
import json
from pathlib import Path
import unittest
import sqlite3

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import job_config,review_candidates,job_db

class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.cfg=job_config.load()
        self.row={'state':'Verified','availability':'Live','checked_at':'2026-09-28T00:00:00Z','requisition_id':'123',
                  'canonical_url':'https://jobs.lever.co/acme/123',
                  'title':'Software Engineer - Java Springboot','location':'Bangalore, India',
                  'jd_text':'The Role: Software Backend Engineer in Java and Spring Boot. The work you will do includes designing, implementing, and maintaining reliable backend services and REST APIs for customers. The qualifications you need: 3-6 years of hands-on software engineering experience. Hands on experience in Java, Spring Boot, Hibernate frameworks, production service development, testing, and support for scalable systems.'}
    def test_experience_gap_is_medium_with_excerpt(self):
        result=review_candidates.assess(self.row,self.cfg)
        self.assertEqual(result['fit'],'Medium')
        self.assertIn('3-6 years',result['evidence']['experience'])
        self.assertFalse(result['eligible_for_resume'])
    def test_no_years_remains_unresolved(self):
        self.row['jd_text']='Software Backend Engineer. The qualifications you need: Hands on experience in Java, Spring Boot and REST APIs.'
        result=review_candidates.assess(self.row,self.cfg)
        self.assertEqual(result['fit'],'Unresolved')
        self.assertFalse(result['eligible_for_resume'])
    def test_outside_india_or_wrong_title_not_review_eligible(self):
        self.row['location']='New York, USA'
        self.assertFalse(review_candidates.eligible(self.row))
        self.row['location']='Bangalore, India';self.row['title']='Account Executive'
        self.assertFalse(review_candidates.eligible(self.row))
    def test_seniority_and_stack_gaps_are_reviewed_not_dropped_at_selection(self):
        self.row['title']='Senior Software Development Engineer'
        self.row['jd_text']='Responsibilities include developing, testing, and maintaining backend services for customers. The qualifications require 5+ years of experience in Go and cloud software engineering. The role builds reliable services and supports production releases with distributed systems and APIs.'
        self.assertTrue(review_candidates.eligible(self.row))
        self.assertEqual(review_candidates.assess(self.row,self.cfg)['fit'],'Low')
    def test_backend_developer_title_is_reviewable(self):
        self.row['title']='Backend Developer'
        self.row['jd_text']='Backend Developer. Responsibilities include designing and building scalable REST APIs and production services. The qualifications require Java and Spring Boot, 2 years of experience, testing, monitoring, and support of distributed software systems in production environments.'
        self.assertTrue(review_candidates.eligible(self.row))
    def test_listing_shell_without_substantive_jd_is_not_reviewable(self):
        self.row['title']='Senior Software Development Engineer'
        self.row['jd_text']='Title: Senior Software Development Engineer\nLocation: Pune, India\n\nSenior Software Development Engineer'
        self.assertFalse(review_candidates.eligible(self.row))
    def test_portal_native_job_id_is_a_review_identity(self):
        self.row['requisition_id']=None
        self.row['provenance_json']=json.dumps({'verification_method':'portal_native','portal':'linkedin','portal_job_id':'4471320992'})
        self.assertTrue(review_candidates.eligible(self.row))
        self.row['provenance_json']=json.dumps({'verification_method':'portal_native','portal':'cutshort','portal_job_id':'mKSZr8Th'})
        self.assertTrue(review_candidates.eligible(self.row))
        self.row['provenance_json']=json.dumps({'verification_method':'portal_native','portal':'cutshort','portal_job_id':'unknown'})
        self.assertFalse(review_candidates.eligible(self.row))
        self.row['provenance_json']=json.dumps({'verification_method':'portal_native','portal':'linkedin','portal_job_id':'4471320992',
                                                'latest_live_jd_status':'TitleOnly'})
        self.assertFalse(review_candidates.eligible(self.row))
    def test_per_company_cap_releases_slots_for_other_employer(self):
        db=sqlite3.connect(':memory:',isolation_level=None);db.row_factory=sqlite3.Row;job_db.initialize(db)
        for company,count in (('Acme',8),('Beta',2)):
            for i in range(count):
                number=f'{company}-{i}'
                job,_=job_db.create_job(db,{'employer':company,'title':'Software Engineer','requisition_id':number,
                    'canonical_url':f'https://jobs.lever.co/{company.lower()}/{number}',
                    'source':company,'location':'Pune, India','discovered_at':job_db.now(),
                    'checked_at':job_db.now(),'availability':'Live','jd_text':self.row['jd_text']})
                job_db.transition(db,job,'Verified','test','original checked')
        ids=[r['id'] for r in db.execute('SELECT id FROM jobs')]
        review_candidates.enqueue(db,ids)
        results=review_candidates.consume(db,self.cfg,limit=6,per_company=3)
        self.assertEqual(sum(x['company']=='Acme' for x in results),3)
        self.assertEqual(sum(x['company']=='Beta' for x in results),2)
        db.close()

if __name__=='__main__':unittest.main()
