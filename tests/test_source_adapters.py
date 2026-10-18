import sys
from pathlib import Path
import unittest
from urllib.error import HTTPError
from unittest.mock import patch
from io import BytesIO

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import source_adapters as a

class SourceTests(unittest.TestCase):
    def test_lever_normalizes_and_reports_partial_at_page_limit(self):
        item={'id':'abc','text':'Java Backend Engineer','hostedUrl':'https://jobs.lever.co/hevo/abc',
              'categories':{'location':'Pune, India'},'descriptionPlain':'Build Java Spring Boot APIs.'}
        config={'enabled':True,'type':'lever','board':'hevo','employer':'Hevo Data','max_pages':1,'page_size':1}
        result=a.fetch_board('hevo_lever',config,get_json=lambda url,host:[item])
        self.assertEqual(result['status'],'Partial')
        self.assertEqual(result['jobs'][0]['requisition_id'],'abc')
        self.assertEqual(result['jobs'][0]['posted_at'],None)

    def test_greenhouse_pagination_and_off_host_rejected(self):
        item={'id':123,'title':'Java Engineer','absolute_url':'https://job-boards.greenhouse.io/celonis/jobs/123',
              'content':'<p>Spring Boot backend in India</p>','location':{'name':'Bengaluru'}}
        config={'enabled':True,'type':'greenhouse','board':'celonis','employer':'Celonis','max_pages':1,'page_size':1}
        result=a.fetch_board('celonis_greenhouse',config,get_json=lambda url,host:{'jobs':[item,item]})
        self.assertEqual(result['status'],'Partial')
        self.assertEqual(result['jobs'][0]['canonical_url'],item['absolute_url'])
        bad=dict(item,absolute_url='https://evil.example/jobs/123')
        result=a.fetch_board('celonis_greenhouse',config,get_json=lambda url,host:{'jobs':[bad]})
        self.assertEqual(result['status'],'Failed')
        self.assertEqual(result['jobs'],[])

    def test_403_is_blocked_not_success(self):
        config={'enabled':True,'type':'ashby','board':'Ontic','employer':'Ontic','max_pages':1,'page_size':10}
        def denied(url,host):raise HTTPError(url,403,'Forbidden',{},None)
        result=a.fetch_board('ontic_ashby',config,get_json=denied)
        self.assertEqual(result['status'],'Blocked')
        self.assertFalse(result['fetch_succeeded'])

    def test_exact_direct_page_requires_substantive_jd(self):
        class Response(BytesIO):
            status=200
            headers={'Content-Type':'text/html'}
        url='https://jobs.lever.co/acme/12345678-1234-1234-1234-123456789012'
        shell=b'<html><head><title>Java Engineer</title></head><body>Java Engineer</body></html>'
        with patch.object(a,'build_opener',return_value=type('Opener',(),{'open':lambda self,*args,**kwargs:Response(shell)})()):
            result=a.verify_direct_requisition(url,'lever')
        self.assertEqual(result['status'],'LiveTitleOnly')
        self.assertFalse(result['verified'])
        body=('Build backend services and APIs. Responsibilities include Java development, REST API design, '
              'testing, code review and production service ownership. Qualifications require Spring Boot experience, '
              'microservices knowledge, database fundamentals and clear communication with a cross-functional team.')
        import json
        page=('<html><script type="application/ld+json">'+json.dumps({'@type':'JobPosting','title':'Java Engineer',
            'description':'<p>'+body+'</p>','datePosted':'2026-09-20','hiringOrganization':{'name':'Acme'}})+
            '</script></html>').encode()
        class Response2(BytesIO):
            status=200
            headers={'Content-Type':'text/html'}
        with patch.object(a,'build_opener',return_value=type('Opener',(),{'open':lambda self,*args,**kwargs:Response2(page)})()):
            result=a.verify_direct_requisition(url,'lever')
        self.assertTrue(result['verified'])
        self.assertEqual(result['employer'],'Acme')
        self.assertIn('Responsibilities',result['jd_text'])

    def test_linked_employer_page_requires_portal_job_link_and_same_host_evidence(self):
        rule={'path_pattern':r'(?i)(?:/jobs?/|/careers?/|/positions?/|/roles?/|job[-_]?details?)'}
        url='https://careers.acme.example/jobs/java-backend-12345'
        body=('Responsibilities include building Spring Boot backend APIs, testing services, and maintaining production systems. '
              'You will work with product and operations partners to improve availability, code quality, and deployment safety. '
              'Qualifications include Java, Spring Boot, REST, SQL, and collaborative software delivery.')
        import json
        page=('<html><script type="application/ld+json">'+json.dumps({'@type':'JobPosting','title':'Java Backend Engineer',
            'description':'<p>'+body+'</p>','datePosted':'2026-09-20','hiringOrganization':{'name':'Acme'}})+
            '</script></html>').encode()
        class Response(BytesIO):
            status=200
            headers={'Content-Type':'text/html'}
        with patch.object(a,'build_opener',return_value=type('Opener',(),{'open':lambda self,*args,**kwargs:Response(page)})()):
            result=a.verify_linked_employer_requisition(url,'www.linkedin.com',rule)
        self.assertTrue(result['verified'])
        self.assertEqual(result['employer'],'Acme')
        self.assertIsNone(result['requisition_id'])
        blocked=a.verify_linked_employer_requisition(url+'?session_token=secret','www.linkedin.com',rule)
        self.assertFalse(blocked['verified'])
        self.assertEqual(blocked['error_kind'],'sensitive_query_parameter_skipped')

if __name__=='__main__':unittest.main()
