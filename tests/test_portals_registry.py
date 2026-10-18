import copy
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'));sys.path.insert(0,str(ROOT/'tests'))
import job_config,job_db
from portals import registry,Capability
from portals.base import NotSupported
from test_auto_pipeline import Fixture

class RegistryTests(Fixture):
    def caps(self,config=None):
        return registry.capabilities(self.db,config or self.config)
    def test_only_naukri_can_apply_and_only_with_switch_and_proof(self):
        config=copy.deepcopy(self.config);config['naukri_worker']['enabled']=True
        with patch.object(job_db,'portal_path_verified',return_value=True):
            caps=self.caps(config)
        self.assertEqual(caps['naukri']['capability'],'ApplyReady')
        for name in ('linkedin','foundit','indeed','cutshort','ats'):self.assertNotEqual(caps[name]['capability'],'ApplyReady',name)
        config['naukri_worker']['enabled']=False
        self.assertEqual(self.caps(config)['naukri']['capability'],'DiscoveryOnly')
        config['naukri_worker']['enabled']=True
        with patch.object(job_db,'portal_path_verified',return_value=False):
            self.assertEqual(self.caps(config)['naukri']['capability'],'DiscoveryOnly')
    def test_restore_pending_or_failing_discovery_blocks(self):
        import naukri_native_worker as lane
        with patch.object(lane,'_marker') as marker:
            marker.return_value.is_file.return_value=True
            self.assertEqual(self.caps()['naukri']['capability'],'Blocked')
        self.db.execute("INSERT INTO runs(id,started_at,mode,status) VALUES('r','2026-01-01','read_only','x')")
        self.db.execute("INSERT INTO source_coverage(run_id,source,page_key,checked_at,status) VALUES('r','foundit','p','2026-01-01T00:00:00+00:00','NoVisibleCards')")
        caps=self.caps();self.assertEqual(caps['foundit']['capability'],'Blocked');self.assertIn('NoVisibleCards',caps['foundit']['reason'])
        self.assertEqual(self.db.execute("SELECT capability FROM portal_capability WHERE portal='foundit'").fetchone()[0],'Blocked')
    def test_non_naukri_apply_is_not_supported_and_lane_lookup_refuses(self):
        with self.assertRaises(NotSupported):registry.workers()['linkedin'].apply_lane(self.db,self.config,test_mode=True,max_submissions=1)
        worker,reason=registry.apply_lane_worker(self.db,self.config,'linkedin');self.assertIsNone(worker)

class ConfigTests(unittest.TestCase):
    def test_apply_enabled_needs_a_proof_marker_for_other_portals(self):
        config=job_config.load();config['portal_workers']['linkedin']['apply_enabled']=True
        with self.assertRaises(ValueError):job_config.validate(config)
    def test_toggle_discovery_writes_validated_config_atomically(self):
        tmp=Path(tempfile.mkdtemp());self.addCleanup(shutil.rmtree,tmp,True)
        path=tmp/'job_search.yaml';shutil.copy(ROOT/'config/job_search.yaml',path)
        data=job_config.set_source_enabled('sources','foundit',False,path)
        self.assertFalse(data['sources']['foundit']['enabled']);self.assertFalse(job_config.load(path)['sources']['foundit']['enabled'])
        with self.assertRaises(ValueError):job_config.set_apply_enabled('linkedin',True,path)
        with self.assertRaises(ValueError):job_config.set_source_enabled('sources','nope',True,path)
        self.assertEqual(path.stat().st_mode & 0o777,0o600)

if __name__=='__main__':unittest.main()
