import sys
from pathlib import Path
import unittest
from unittest.mock import patch
import sqlite3

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import portal_native
import job_db

class PortalNativeSelectionTests(unittest.TestCase):
    def test_generic_java_card_is_selected_and_each_second_stage_skip_is_recorded(self):
        leads=[('naukri',{'title':'Software Engineer','portal_job_id':'12345678','card_skills':['Java','Spring Boot']}),
               ('naukri',{'title':'SDE','portal_job_id':'12345679','card_skills':['Java','REST']}),
               ('naukri',{'title':'QA Automation','portal_job_id':'12345680','card_skills':['Java']})]
        selected,skips=portal_native.selection_plan(leads,limit=1,per_company=5)
        self.assertEqual(len(selected),1)
        self.assertEqual({x['reason'] for x in skips},{'unrelated_bpo_data_entry_qa_role','per_run_jd_cap'})
        self.assertEqual(len(skips),2)
    def test_recheck_refreshes_same_untouched_job_but_never_a_locked_attempt(self):
        db=sqlite3.connect(':memory:',isolation_level=None);db.row_factory=sqlite3.Row;job_db.initialize(db)
        lead={'title':'Java Developer','portal_job_id':'12345678','employer':'Acme','card_location':'Pune, India',
              'url':'https://www.naukri.com/job-listings-java-developer-acme-12345678'}
        jd=('Responsibilities: Java and Spring Boot backend REST API development. Requirements: '
            '2 years experience developing reliable production services and collaborating with engineers. ')*3
        obs={'host':'www.naukri.com','url':lead['url'],'title':'Java Developer','employer':'Acme',
             'location':'Pune, India','jd':jd,'body_characters':1000,'apply_visible':True}
        with patch('portal_native.inspect',return_value=obs):
            first=portal_native.verify_leads(db,[('naukri',lead)])
        job=first['verified_job_ids'][0]
        with patch('portal_native.inspect',return_value={**obs,'jd':jd+' Additional requirements: SQL.'}):
            refreshed=portal_native.verify_leads(db,[('naukri',lead)])
        self.assertEqual(refreshed['verified_job_ids'],[job])
        self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
        self.assertIn('Additional requirements',db.execute('SELECT jd_text FROM jobs').fetchone()[0])
        db.execute("UPDATE jobs SET state='SubmissionUnconfirmed' WHERE id=?",(job,))
        before=db.execute('SELECT jd_text FROM jobs').fetchone()[0]
        with patch('portal_native.inspect',return_value={**obs,'jd':jd+' Must not overwrite locked job.'}):
            locked=portal_native.verify_leads(db,[('naukri',lead)])
        self.assertEqual(locked['verified'],0);self.assertEqual(db.execute('SELECT jd_text FROM jobs').fetchone()[0],before)
        db.close()

    def test_deduplicates_card_ids_and_caps_employers_across_sources(self):
        leads=[('naukri',{'title':'Java Spring Boot Developer','portal_job_id':str(10000000+i),
                          'employer':'Acme','card_excerpt':'2-3 years'}) for i in range(6)]
        leads += [('cutshort',{'title':'Java Backend Engineer','portal_job_id':f'abc123{i:02d}',
                               'card_excerpt':f'Java Backend Engineer\nat Beta {i}\n2 years'}) for i in range(3)]
        leads.append(leads[0])
        chosen=portal_native.select(leads,limit=5,per_company=2)
        self.assertEqual(len(chosen),5)
        self.assertEqual(sum(source=='naukri' for source,_ in chosen),2)
        self.assertEqual(len({(source,lead['portal_job_id']) for source,lead in chosen}),5)

    def test_same_employer_and_title_keep_distinct_portal_requisitions(self):
        db=sqlite3.connect(':memory:',isolation_level=None);db.row_factory=sqlite3.Row;job_db.initialize(db)
        leads=[('naukri',{'title':'Java Developer','portal_job_id':str(12345678+i),
                          'employer':'Acme','card_location':'Pune, India',
                          'url':f'https://www.naukri.com/job-listings-java-developer-acme-{12345678+i}'})
               for i in range(2)]
        jd=('Responsibilities include designing and building backend REST APIs using Java and Spring Boot. '
            'Requirements include 2 years of experience developing and testing production services, '
            'collaborating with product engineers, and maintaining reliable software systems. ')*2
        def observed(source,lead):
            return {'host':'www.naukri.com','url':lead['url'],'title':'Java Developer','employer':'Acme',
                    'location':'Pune, India','jd':jd,'body_characters':1000,'apply_visible':True}
        with patch('portal_native.inspect',side_effect=observed):
            result=portal_native.verify_leads(db,leads,limit=2,per_company=2)
        self.assertEqual(result['verified'],2)
        slugs=[r[0] for r in db.execute("SELECT slug FROM jobs WHERE source='naukri_portal_native'")]
        self.assertEqual(len(set(slugs)),2)
        db.close()

if __name__=='__main__':unittest.main()
