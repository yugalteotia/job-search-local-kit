import json
import sys
from pathlib import Path
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import browser_portals,job_config

class Fake:
    result={}
    calls=[]
    def __init__(self):pass
    def tool(self,name,args,timeout=30):
        self.calls.append((name,args.get('action')))
        if name=='browser_evaluate':return {'content':[{'type':'text','text':json.dumps(self.result)}]}
        if name=='browser_tabs' and args.get('action')=='new':
            return {'content':[{'type':'text','text':'### Result\n- 0: (current) [automation tab](https://example.invalid/)'}]}
        return {'content':[]}
    def close(self):pass

class PortalTests(unittest.TestCase):
    def setUp(self):
        Fake.calls=[]
        cfg=job_config.load();self.search=cfg['search'];self.source=cfg['sources']['linkedin']
    def test_bounded_page_is_partial_and_does_not_claim_original(self):
        Fake.result={'host':'www.linkedin.com','url':'https://www.linkedin.com/jobs/search/',
                     'title':'Jobs','blocked':False,'links':[{'title':'Java Engineer','url':'https://www.linkedin.com/jobs/view/123'}]}
        r=browser_portals.fetch_portal('linkedin',self.source,self.search,page_cap=1,query_cap=1,client_factory=Fake)
        self.assertEqual(r['status'],'Partial')
        self.assertEqual(r['requested_pages'],6)
        self.assertEqual(r['successful_pages'],1)
        self.assertNotIn('jobs',r)
        self.assertIn(('browser_tabs','new'),Fake.calls)
        self.assertIn(('browser_tabs','close'),Fake.calls)
        self.assertEqual(r['tab_lifecycle']['created'],1)
        self.assertEqual(r['tab_lifecycle']['closed'],1)
        self.assertEqual(r['tab_lifecycle']['remaining_automation_tabs'],0)
    def test_verification_page_blocks_source(self):
        Fake.result={'host':'www.linkedin.com','blocked':True,'links':[]}
        r=browser_portals.fetch_portal('linkedin',self.source,self.search,page_cap=1,query_cap=1,client_factory=Fake)
        self.assertEqual(r['status'],'Blocked')
        self.assertEqual(r['error_kind'],'site_verification')
    def test_off_host_redirect_is_not_coverage(self):
        Fake.result={'host':'other.example','blocked':False,'links':[]}
        r=browser_portals.fetch_portal('linkedin',self.source,self.search,page_cap=1,query_cap=1,client_factory=Fake)
        self.assertEqual(r['status'],'Blocked')
        self.assertFalse(r['fetch_succeeded'])

    def test_loaded_portal_with_no_visible_cards_is_not_success(self):
        source=job_config.load()['sources']['naukri']
        Fake.result={'host':'www.naukri.com','title':'Jobs','blocked':False,
                     'visible_cards':0,'body_characters':200,'links':[]}
        r=browser_portals.fetch_portal('naukri',source,self.search,page_cap=1,query_cap=1,client_factory=Fake)
        self.assertEqual(r['status'],'NoVisibleCards')
        self.assertEqual(r['pages'][0]['visible_cards'],0)
        self.assertFalse(r['fetch_succeeded'])

    def test_visible_cards_without_exact_urls_are_unresolved_coverage(self):
        source=job_config.load()['sources']['cutshort']
        Fake.result={'host':'cutshort.io','title':'Jobs','blocked':False,
                     'visible_cards':4,'body_characters':300,'links':[]}
        r=browser_portals.fetch_portal('cutshort',source,self.search,page_cap=1,query_cap=1,client_factory=Fake)
        self.assertEqual(r['status'],'CardsWithoutExactLinks')
        self.assertEqual(r['pages'][0]['error_kind'],'job_specific_url_not_visible')
        self.assertFalse(r['fetch_succeeded'])

    def test_exact_cutshort_card_keeps_source_and_search_provenance(self):
        source=job_config.load()['sources']['cutshort']
        Fake.result={'host':'cutshort.io','title':'Java jobs','blocked':False,'visible_cards':1,
                     'links':[{'title':'Java Developer','url':'https://cutshort.io/job/Java-Developer-abc12345',
                               'portal_job_id':'abc12345'}]}
        r=browser_portals.fetch_portal('cutshort',source,self.search,page_cap=1,query_cap=1,client_factory=Fake)
        self.assertEqual(r['pages'][0]['visible_cards'],1)
        self.assertEqual(r['leads'][0]['portal_job_id'],'abc12345')
        self.assertEqual(r['leads'][0]['source'],'cutshort')
        self.assertEqual(r['leads'][0]['search_page_url'],r['pages'][0]['url'])

if __name__=='__main__':unittest.main()
