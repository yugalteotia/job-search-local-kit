import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,MagicMock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import core_run,job_config,pipeline_v2,progress

class ManualRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp=Path(tempfile.mkdtemp());self.addCleanup(shutil.rmtree,self.tmp,True)
        self.config=job_config.load();self.config['schedule']['enabled']=False
    def run_pipeline(self,**kw):
        with patch.object(pipeline_v2,'STATE',self.tmp),patch.object(pipeline_v2.job_config,'load',return_value=self.config),\
             patch.object(pipeline_v2,'_locked',return_value={'status':'Success','errors':[]}):
            return pipeline_v2.run(scheduled=True,test=True,**kw)
    def test_manual_run_works_with_timer_disabled_but_timer_run_does_not(self):
        self.assertEqual(self.run_pipeline()['status'],'Disabled')
        self.assertEqual(self.run_pipeline(manual=True)['status'],'Success')
    def test_pause_blocks_manual_runs_and_lock_blocks_overlap(self):
        (self.tmp/'control.json').write_text(json.dumps({'paused':True}))
        self.assertEqual(self.run_pipeline(manual=True)['status'],'Paused')
        (self.tmp/'control.json').write_text(json.dumps({'paused':False}))
        import fcntl
        with (self.tmp/'daily-run.lock').open('a') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertEqual(self.run_pipeline(manual=True)['status'],'OverlapPrevented')
    def test_can_start_reports_pause_and_active_run(self):
        with patch('control.is_paused',return_value=True):self.assertFalse(core_run.can_start()[0])
        with patch('control.is_paused',return_value=False),patch.object(core_run,'lock_held',return_value=True):
            self.assertEqual(core_run.can_start(),(False,'Another run is active'))
    def test_detached_start_uses_transient_unit_with_service_path_and_trigger(self):
        done=MagicMock(returncode=0)
        with patch.object(core_run,'can_start',return_value=(True,'')),patch('subprocess.run',return_value=done) as run,\
             patch('shutil.which',return_value='/usr/bin/systemd-run'):
            out=core_run.start_detached(dry_run=True)
        cmd=run.call_args[0][0]
        self.assertTrue(out['started']);self.assertEqual(out['via'],'systemd-run')
        self.assertIn('--user',cmd);self.assertIn('JOB_SEARCH_TRIGGER=ui',cmd);self.assertEqual(cmd[-2:],['apply-now','--dry-run'])
        self.assertTrue(any(x.startswith('PATH=') for x in cmd))
    def test_detached_start_refuses_when_not_startable(self):
        with patch.object(core_run,'can_start',return_value=(False,'Another run is active')):
            self.assertEqual(core_run.start_detached()['started'],False)

class ProgressTests(unittest.TestCase):
    def test_events_are_ordered_readable_incrementally_and_finish_marks_status(self):
        tmp=Path(tempfile.mkdtemp());self.addCleanup(shutil.rmtree,tmp,True)
        with patch.object(progress,'STATE',tmp):
            progress.start('run-1',trigger='ui',dry_run=True);progress.emit('step one');progress.emit('step two')
            first=progress.read();self.assertEqual([e['message'] for e in first['events']][-2:],['step one','step two'])
            self.assertEqual(progress.read(since=first['next'])['events'],[])
            progress.finish('Success');done=progress.read('run-1')
            self.assertEqual(done['run']['status'],'Success');self.assertEqual(done['events'][-1]['kind'],'finish')

if __name__=='__main__':unittest.main()
