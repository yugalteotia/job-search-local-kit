import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import candidate,job_config,naukri_native_worker as worker

class Controls(unittest.TestCase):
    def live(self):
        cfg=job_config.load();cfg['mode']='production';cfg['submissions']['enabled']=True
        cfg['naukri_worker']['enabled']=True;cfg['portal_workers']['naukri']['apply_enabled']=True
        cfg['schedule']['application_enabled']=False
        return cfg
    def test_modes_and_timer_have_explicit_gates(self):
        c=self.live();job_config.validate(c)
        self.assertTrue(job_config.live_naukri_enabled(c))
        self.assertFalse(job_config.live_naukri_enabled(c,test_mode=True))
        self.assertFalse(job_config.live_naukri_enabled(c,timer=True))
        c['schedule']['application_enabled']=True
        self.assertTrue(job_config.live_naukri_enabled(c,timer=True))
        for field in ('mode','submissions','naukri_worker','portal_workers'):
            d=copy.deepcopy(c)
            if field=='mode':d[field]='read_only'
            elif field=='portal_workers':d[field]['naukri']['apply_enabled']=False
            else:d[field]['enabled']=False
            self.assertFalse(job_config.live_naukri_enabled(d),field)
    def test_readonly_worker_cannot_open_browser_or_recover_upload(self):
        cfg=self.live();cfg['mode']='read_only';cfg['submissions']['enabled']=False
        with patch.object(worker,'_chrome',side_effect=AssertionError('browser must not open')):
            self.assertEqual(worker.run(None,cfg)['status'],'DiscoveryOnly')
    def test_duration_stops_at_employment_end_and_does_not_invent_total(self):
        import datetime as dt
        e=candidate.professional_experience(dt.date(2026,10,13))
        self.assertEqual(e['completed_months'],23);self.assertEqual(e['years_decimal'],'1.96')
        self.assertFalse(e['exact_total_known'])
        profile=(candidate.ROOT/'APPLICATION_PROFILE.md').read_text()
        self.assertEqual(candidate.answer('When can you join?',profile,today=dt.date(2026,10,13))[0],'Immediate')
        self.assertEqual(candidate.answer('How many years of total experience?',profile),(None,None))

    def test_claim_precedes_a_native_apply_with_no_questionnaire(self):
        from naukri_phase_guard import NaukriPhaseGuard,APPLY_PATH
        g=NaukriPhaseGuard('1234567890');g.claim_before_open('one-attempt')
        packet={'flowtype':'show','strJobsarr':[{'jobId':'1234567890'}]}
        ok,event=g.decide('POST','https://www.naukri.com'+APPLY_PATH,packet)
        self.assertTrue(ok);g.response(event,200,{'job_id_match':True,'isLeafNode':True})
        packet['flowtype']='apply'
        self.assertTrue(g.decide('POST','https://www.naukri.com'+APPLY_PATH,packet)[0])
        self.assertFalse(g.decide('POST','https://www.naukri.com'+APPLY_PATH,packet)[0])

    def test_unclaimed_show_is_blocked_because_it_can_submit_a_leaf_job(self):
        from naukri_phase_guard import NaukriPhaseGuard,APPLY_PATH
        g=NaukriPhaseGuard('1234567890')
        self.assertFalse(g.decide('POST','https://www.naukri.com'+APPLY_PATH,
            {'flowtype':'show','strJobsarr':[{'jobId':'1234567890'}]})[0])

    def test_history_autoloaded_final_page_does_not_click_hidden_sentinel(self):
        page=MagicMock();source=MagicMock();source.filter.return_value=source;source.count.return_value=1
        rows=MagicMock();rows.count.return_value=221;more=MagicMock();more.is_visible.return_value=False
        page.locator.side_effect=lambda s:{'.apply-filters li':source,
            '.jdTuplesContainer .jdTupleContainer':rows,'#loadMoreLnk':more}[s]
        page.evaluate.side_effect=[{'rows':200,'total':221,'exact':0,'company_role':0,'more':True},None,
            {'rows':221,'total':221,'exact':0,'company_role':0,'more':False}]
        result=worker._history(page,'1234567890','Example','Java Developer')
        self.assertTrue(result['full']);self.assertEqual(result['exact'],0);more.click.assert_not_called()

if __name__=='__main__':unittest.main()
