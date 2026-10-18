import sqlite3
import sys
from pathlib import Path
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import job_db,pipeline_v2

class PortalCardPersistenceTests(unittest.TestCase):
    def test_exact_and_unresolved_cards_keep_source_ids_without_duplicate_rows(self):
        db=sqlite3.connect(':memory:',isolation_level=None);db.row_factory=sqlite3.Row;job_db.initialize(db)
        response={'checked_at':'2026-09-28T12:00:00+00:00',
            'leads':[{'portal_job_id':'12345678','url':'https://www.naukri.com/job-listings-java-12345678',
                      'title':'Java Developer','search_page_url':'https://www.naukri.com/java-jobs'}],
            'pages':[{'url':'https://www.naukri.com/java-jobs',
                      'unresolved_cards':[{'portal_job_id':'99999999','reason':'no_job_specific_url_in_selected_detail'}]}]}
        pipeline_v2.record_portal_cards(db,'naukri',response)
        pipeline_v2.record_portal_cards(db,'naukri',response)
        rows=[dict(x) for x in db.execute('SELECT * FROM portal_card_leads ORDER BY portal_job_id')]
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['status'],'ExactURL')
        self.assertEqual(rows[1]['status'],'NeedsExactURL')
        self.assertEqual(rows[1]['failure_reason'],'no_job_specific_url_in_selected_detail')
        db.close()

if __name__=='__main__':unittest.main()
