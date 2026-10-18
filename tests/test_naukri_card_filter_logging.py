"""Mocked discovery only: retain rejection evidence without weakening filters."""
import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import naukri_discovery as discovery


class CardFilterLogging(unittest.TestCase):
    def test_keeps_raw_exact_ids_and_rejection_reasons(self):
        links=[{'portal_job_id':'12345678','url':'https://www.naukri.com/job-listings-java-12345678','title':'Java Developer','card_excerpt':'2 - 3 Yrs'},
               {'portal_job_id':'12345679','url':'https://www.naukri.com/job-listings-java-12345679','title':'Java Developer','card_excerpt':'4 - 6 Yrs'},
               {'portal_job_id':'12345680','url':'https://www.naukri.com/job-listings-accountant-12345680','title':'Accountant','card_excerpt':'2 - 3 Yrs'}]
        page=MagicMock();ctx=MagicMock();ctx.pages=[page]
        page.evaluate.side_effect=lambda script:{'url':'https://www.naukri.com/java-jobs','host':'www.naukri.com','visible_cards':4,'links':links} if script==discovery.CARD_DOM['naukri'] else {'keywords':'Java, ','location':'India, '}
        def on_request(event,callback):
            request=MagicMock();request.url='https://www.naukri.com/jobapi/v3/search?keyword=Java&location=India&experience=2&jobAge=7&sort=f'
            callback(request)
        page.on.side_effect=on_request
        def locator(selector):
            loc=MagicMock();loc.bounding_box.return_value={'x':0,'y':0,'width':300,'height':10}
            loc.inner_text.return_value={'#filter-freshness':'Last 7 days','#filter-sort':'Date',
                '[data-filter-id="experience"] .handle span':'2'}.get(selector,'')
            return loc
        page.locator.side_effect=locator
        @contextlib.contextmanager
        def chrome(**kwargs):yield ctx
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'.workflow/runs').mkdir(parents=True)
            with patch.object(discovery,'ROOT',root),patch.object(discovery,'_chrome',chrome),\
                 patch.object(discovery,'_status',return_value={'signed_in':True}),patch.object(discovery,'_routes',return_value=[('Java','India')]):
                result=discovery.fetch({'max_pages':1,'search_url':'https://www.naukri.com/{query}?l={location}'},{},page_cap=1,query_cap=1)
            saved=json.loads(next((root/'.workflow/runs').glob('*-naukri-search.json')).read_text())
            self.assertEqual(len(result['leads']),1);self.assertEqual(len(saved['raw_cards']),3)
            self.assertEqual(saved['pages'][0]['extracted_exact_ids'],['12345678','12345679','12345680'])
            self.assertEqual(saved['pages'][0]['card_filter_exclusions'],{'experience_minimum_at_least_four':1,'no_relevant_title_or_generic_engineering_card_skills':1})
            self.assertEqual(saved['cards'][0]['portal_job_id'],'12345678')

    def test_generic_engineering_cards_use_separate_relevant_skill_tags(self):
        for title in ('Software Engineer','SDE','Software Developer','Forward Deployed Software Engineer'):
            self.assertIsNone(discovery.card_filter_reason({'title':title,'card_skills':['Java','Microservices'],'card_excerpt':'2-3 Yrs'}))
        for title in ('Backend Operations/Data Entry/BPO','QA Automation Engineer','API Testing Engineer'):
            self.assertEqual(discovery.card_filter_reason({'title':title,'card_skills':['Java'],'card_excerpt':'2-3 Yrs'}),'unrelated_bpo_data_entry_qa_role')
        self.assertIsNotNone(discovery.card_filter_reason({'title':'Software Engineer','card_skills':['JavaScript','CSS'],'card_excerpt':'2-3 Yrs'}))

    def test_rendered_keywords_location_and_api_filters_must_agree(self):
        self.assertTrue(discovery.rendered_search_matches('Java Spring Boot','India',{'keywords':'java spring boot, ','location':'india, '}))
        self.assertFalse(discovery.rendered_search_matches('Java Spring Boot','India',{'keywords':'java','location':'Mumbai'}))
        p={'keyword':['java spring boot'],'location':['india'],'experience':['2','2'],'jobAge':['7','7'],'sort':['f']}
        self.assertTrue(discovery.api_search_matches('Java Spring Boot','India',2,7,p))
        for key in p:
            bad={**p,key:['wrong']};self.assertFalse(discovery.api_search_matches('Java Spring Boot','India',2,7,bad))


if __name__=='__main__':unittest.main()
