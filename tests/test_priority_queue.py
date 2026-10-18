import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import priority_queue as q

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Path(self.tmp.name)/'queue.sqlite3'
        self.t=dt.datetime.now(dt.timezone.utc).isoformat()
    def tearDown(self):self.tmp.cleanup()
    def add(self,slug,company,req,host,priority):
        return q.enqueue(slug,company,req,f'https://{host}/jobs/{req}',priority,self.t,self.db)
    def test_atomic_claim_and_priority(self):
        self.add('lower','A','1','a.example',50)
        self.add('higher','B','2','b.example',90)
        with ThreadPoolExecutor(max_workers=3) as pool:
            rows=list(pool.map(lambda i:q.claim(f'w{i}',self.db),range(3)))
        self.assertEqual({r['slug'] for r in rows if r}, {'lower','higher'})
        self.assertEqual(sum(r is None for r in rows),1)
        self.assertEqual(q.status(self.db)['counts']['resuming'],2)
    def test_same_employer_or_ats_is_serialized(self):
        self.add('one','A','1','ats.example',90)
        self.add('two','A','2','other.example',80)
        self.add('three','B','3','ats.example',70)
        first=q.claim('w1',self.db)
        self.assertEqual(first['slug'],'one')
        self.assertIsNone(q.claim('w2',self.db))
        q.transition(first['id'],'w1','resume_ready',path=self.db)
        self.assertEqual(q.claim('w2',self.db)['slug'],'two')
    def test_release_after_block_and_no_reclaim(self):
        self.add('one','A','1','ats.example',90)
        self.add('two','A','2','ats.example',80)
        first=q.claim('w1',self.db)
        q.transition(first['id'],'w1','blocked',path=self.db)
        self.assertEqual(q.claim('w2',self.db)['slug'],'two')
        self.assertIsNone(q.claim('w3',self.db))
    def test_submission_stage_requires_owner_and_proof(self):
        self.add('one','A','1','ats.example',90)
        row=q.claim('resume',self.db)
        with self.assertRaises(ValueError):q.transition(row['id'],'other','resume_ready',path=self.db)
        q.transition(row['id'],'resume','resume_ready',path=self.db)
        form=q.claim('form',self.db,stage='form')
        with self.assertRaises(ValueError):q.transition(form['id'],'form','submitted',path=self.db)
        q.transition(form['id'],'form','submitting',path=self.db)
        q.transition(form['id'],'form','submitted',path=self.db)
        self.assertIsNone(q.claim('again',self.db))
        with self.assertRaises(ValueError):self.add('alias','A','1','ats.example',90)
    def test_cooldown_and_no_stale_recycling(self):
        self.add('one','A','1','ats.example',90)
        q.cooldown('ats.example',300,'rate_limit',self.db)
        self.assertIsNone(q.claim('w1',self.db))
        self.add('two','B','2','else.example',80)
        self.assertEqual(q.claim('w2',self.db)['slug'],'two')

if __name__=='__main__':unittest.main()
