import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import linkedin_browser

class Page:
    def __init__(self,counts,path='/feed/',password=False,extra=None):self.counts=counts;self.i=0;self.url='https://www.linkedin.com'+path;self.password=password;self.extra=extra or {}
    def goto(self,*a,**k):pass
    def wait_for_selector(self,*a,**k):pass
    def wait_for_timeout(self,*a):self.i+=1
    def evaluate(self,*a):return {"ready_state":"complete",**self.extra}
    def locator(self,selector):
        page=self
        class Locator:
            def count(self):return int(page.password) if 'password' in selector else page.counts[min(page.i,len(page.counts)-1)]
        return Locator()

class SessionHealth(unittest.TestCase):
    def test_delayed_feed_controls_require_stable_positive_evidence(self):
        result=linkedin_browser.status(Page([0,0,1,1]))
        self.assertTrue(result['signed_in']);self.assertEqual(len(result['health_samples']),4)
    def test_missing_feed_links_is_render_failure_not_login(self):
        result=linkedin_browser.status(Page([0]))
        self.assertFalse(result['signed_in']);self.assertFalse(result['authentication_required']);self.assertFalse(result['verification_required'])
        self.assertEqual(result['error_kind'],'linkedin_selector_or_render_failure')
    def test_loading_is_distinct_from_selector_failure(self):
        result=linkedin_browser.status(Page([0],extra={'ready_state':'loading'}))
        self.assertEqual(result['error_kind'],'linkedin_page_loading');self.assertFalse(result['authentication_required'])
    def test_other_authenticated_controls_can_verify_session(self):
        result=linkedin_browser.status(Page([0],extra={'authenticated_controls':1}))
        self.assertTrue(result['signed_in']);self.assertEqual(result['session_state'],'authenticated')
    def test_visible_challenge_and_restriction_stop_even_with_feed_links(self):
        for extra,state in [({'captcha_visible':True},'verification_required'),({'restriction_heading':True},'account_restricted')]:
            result=linkedin_browser.status(Page([1],extra=extra))
            self.assertFalse(result['signed_in']);self.assertEqual(result['session_state'],state)
    def test_login_or_challenge_never_becomes_positive(self):
        for path,password in [('/login',True),('/checkpoint/challenge',False),('/feed/',True)]:
            result=linkedin_browser.status(Page([1],path,password))
            self.assertFalse(result['signed_in'])
        self.assertTrue(linkedin_browser.status(Page([0],'/checkpoint/challenge'))['verification_required'])

class DiscoveryHealth(unittest.TestCase):
    def test_discovery_preserves_specific_reason_without_searching(self):
        import contextlib,tempfile
        from unittest.mock import MagicMock,patch
        import linkedin_discovery
        for state,error in [('selector_or_render_failure','linkedin_selector_or_render_failure'),
                            ('authentication_required','linkedin_authentication_required'),
                            ('verification_required','linkedin_verification_required')]:
            with tempfile.TemporaryDirectory() as temp:
                root=Path(temp);(root/'.workflow/runs').mkdir(parents=True)
                browser=MagicMock();browser.pages=[MagicMock()]
                @contextlib.contextmanager
                def chrome(**options):yield browser
                health={'signed_in':False,'session_state':state,'error_kind':error}
                with patch.object(linkedin_discovery,'ROOT',root),patch.object(linkedin_discovery,'chrome',chrome),\
                     patch.object(linkedin_discovery,'status',return_value=health),\
                     patch.object(linkedin_discovery,'_routes',side_effect=AssertionError('No search allowed')):
                    result=linkedin_discovery.fetch({}, {},session_check_only=True)
                self.assertEqual(result['error_kind'],error);self.assertEqual(result['leads'],[])

if __name__=='__main__':unittest.main()
