import copy
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import freshness,job_config,job_db

class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.cfg=copy.deepcopy(job_config.load())
        self.now=dt.datetime(2026,9,28,12,0,tzinfo=dt.timezone.utc)
        self.cfg['sources']['naukri']['last_successful_fetch']='2026-09-28T09:00:00+00:00'
    def test_calendar_date_and_missing_date_remain_unknown(self):
        for value in ('2026-09-28',None,'Posted 3 hours ago'):
            self.assertEqual(freshness.classify(value,'2026-09-28T09:00:00+00:00',self.cfg['freshness'],self.now)[0],'Unknown')
    def test_explicit_posting_clock_drives_window_and_sweep_priority(self):
        policy=self.cfg['freshness'];last='2026-09-28T09:00:00+00:00'
        self.assertEqual(freshness.classify('2026-09-28T08:30:00+00:00',last,policy,self.now)[0],'Fresh')
        self.assertEqual(freshness.classify('2026-09-28T03:00:00+00:00',last,policy,self.now)[0],'Recent')
        self.assertEqual(freshness.classify('2026-09-27T10:00:00+00:00',last,policy,self.now)[0],'Old')
    def test_unchanged_reviewed_url_is_skipped_before_detail(self):
        db=sqlite3.connect(':memory:',isolation_level=None);db.row_factory=sqlite3.Row;job_db.initialize(db)
        lead={'url':'https://www.naukri.com/job-listings-java-acme-12345678','portal_job_id':'12345678',
              'title':'Java Developer','posted_at':None}
        digest=freshness.fingerprint('naukri',lead)
        db.execute('INSERT INTO lead_checks(canonical_url,status,checked_at,result_json) VALUES(?,?,?,?)',
                   (lead['url'],'PortalNativeVerified','2026-09-28T10:00:00+00:00',json.dumps({'card_sha256':digest})))
        kept,counts,reasons=freshness.select(db,[('naukri',lead)],self.cfg,now=self.now)
        self.assertEqual(kept,[])
        self.assertEqual(counts['repeat_skipped'],1)
        self.assertEqual(reasons[0]['reason'],'unchanged_already_reviewed')
        db.close()
    def test_explicit_old_post_is_skipped_before_detail(self):
        db=sqlite3.connect(':memory:',isolation_level=None);db.row_factory=sqlite3.Row;job_db.initialize(db)
        lead={'url':'https://www.naukri.com/job-listings-java-acme-12345678',
              'portal_job_id':'12345678','title':'Java Developer',
              'posted_at':'2026-09-27T10:00:00+00:00'}
        kept,counts,reasons=freshness.select(db,[('naukri',lead)],self.cfg,now=self.now)
        self.assertEqual(kept,[])
        self.assertEqual(counts['skipped_old'],1)
        self.assertEqual(reasons[0]['reason'],'posting_older_than_routine_review_window')
        db.close()
    def test_unchanged_reviewed_lead_gets_a_bounded_recheck_after_deadline(self):
        db=sqlite3.connect(':memory:',isolation_level=None);db.row_factory=sqlite3.Row;job_db.initialize(db)
        lead={'url':'https://www.naukri.com/job-listings-java-acme-12345678','portal_job_id':'12345678',
              'title':'Java Developer','posted_at':None}
        db.execute('INSERT INTO lead_checks VALUES(?,?,?,?)',(lead['url'],'PortalNativeVerified',
                   '2026-09-27T10:00:00+00:00',json.dumps({'card_sha256':freshness.fingerprint('naukri',lead)})))
        kept,counts,_=freshness.select(db,[('naukri',lead)],self.cfg,now=self.now)
        self.assertEqual(len(kept),1);self.assertEqual(counts['repeat_skipped'],0)
        changed={**lead,'card_excerpt':'Requirements changed: Java and Spring Boot'}
        self.assertNotEqual(freshness.fingerprint('naukri',lead),freshness.fingerprint('naukri',changed))
        db.close()
    def test_pending_application_is_defined_repeat_recheck_reason(self):
        db=sqlite3.connect(':memory:',isolation_level=None);db.row_factory=sqlite3.Row;job_db.initialize(db)
        lead={'url':'https://www.naukri.com/job-listings-java-acme-12345678',
              'portal_job_id':'12345678','title':'Java Developer','posted_at':None}
        job_id,_=job_db.create_job(db,{'employer':'Acme','title':'Java Developer',
            'canonical_url':lead['url'],'source':'naukri_portal_native','discovered_at':job_db.now()})
        job_db.transition(db,job_id,'Verified','test','exact JD checked')
        job_db.transition(db,job_id,'Shortlisted','test','pilot pending')
        db.execute('INSERT INTO lead_checks(canonical_url,status,checked_at,result_json) VALUES(?,?,?,?)',
                   (lead['url'],'PortalNativeVerified','2026-09-28T04:00:00+00:00',
                    json.dumps({'card_sha256':freshness.fingerprint('naukri',lead)})))
        kept,counts,_=freshness.select(db,[('naukri',lead)],self.cfg,now=self.now)
        self.assertEqual(len(kept),1)
        self.assertEqual(counts['repeat_skipped'],0)
        db.close()

if __name__=='__main__':unittest.main()
