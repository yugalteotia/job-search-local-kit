import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import ui_api,ui_server

class UITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ui_server.make_server(0,'unit-token');cls.port=cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever,daemon=True).start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close()
    def call(self,path,body=None,token='unit-token',headers=None):
        req=urllib.request.Request(f'http://127.0.0.1:{self.port}{path}',data=None if body is None else json.dumps(body).encode(),
                                   headers={**({'X-UI-Token':token} if token else {}),**(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:return r.status,r.read()
        except urllib.error.HTTPError as e:return e.code,e.read()
    def test_static_shell_is_public_but_data_needs_the_token(self):
        self.assertEqual(self.call('/',token=None)[0],200)
        for path in ('/api/status','/api/handoffs','/api/portals','/api/tracker','/api/progress'):
            self.assertEqual(self.call(path,token=None)[0],401,path);self.assertEqual(self.call(path,token='wrong')[0],401,path)
    def test_host_and_origin_are_checked(self):
        self.assertEqual(self.call('/api/status',headers={'Host':'evil.example'})[0],400)
        self.assertEqual(self.call('/api/pause',body={},headers={'Origin':'http://evil.example'})[0],403)
    def test_no_endpoint_leaks_contact_details_or_reusable_answers(self):
        bodies=[]
        for path in ('/api/status','/api/handoffs','/api/portals','/api/tracker?view=detailed','/api/progress'):
            code,body=self.call(path);self.assertEqual(code,200,path);bodies.append(body.decode())
        joined=' '.join(bodies)
        for secret in ('9560369538','teotia.yugal@gmail.com','linkedin.com/in/yugalteotia','Immediate joiner','LPA'):
            self.assertFalse(secret in joined,'A private value or reusable answer leaked from a dashboard endpoint')
    def test_evidence_is_allowlisted_and_scrubbed(self):
        for bad in ('/api/evidence/../etc/passwd','/api/evidence/naukri-070926003879/../../config/candidate.yaml',
                    '/api/evidence/naukri-070926003879/naukri-native-presubmit.json','/api/evidence/naukri-070926003879/x.py'):
            self.assertNotEqual(self.call(bad)[0],200,bad)
        code,body=self.call('/api/evidence/naukri-070926003879');self.assertEqual(code,200)
        self.assertNotIn('naukri-native-presubmit.json',body.decode())
        code,body=self.call('/api/evidence/naukri-070926003879/naukri-native-confirmation.json');self.assertEqual(code,200)
        self.assertIn('requisition_id',body.decode())
    def test_actions_use_shared_core_and_refuse_unproven_portals(self):
        with patch.object(ui_api.core_run,'start_detached',return_value={'started':False,'reason':'Another run is active'}) as start:
            code,body=self.call('/api/run',{'dry_run':True});self.assertEqual(code,200);start.assert_called_once_with(dry_run=True,trigger='ui')
        with patch('job_db.portal_path_verified',return_value=False):
            code,body=self.call('/api/portals/apply',{'portal':'linkedin','enabled':True});self.assertEqual(code,409)
        self.assertEqual(self.call('/api/nope',{})[0],404)
    def test_oversized_and_malformed_bodies_are_rejected(self):
        self.assertEqual(self.call('/api/pause',{'x':'a'*5000})[0],413)
        req=urllib.request.Request(f'http://127.0.0.1:{self.port}/api/pause',data=b'{bad',headers={'X-UI-Token':'unit-token'})
        with self.assertRaises(urllib.error.HTTPError) as ctx:urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code,400)
    def test_server_binds_localhost_only(self):
        self.assertEqual(self.server.server_address[0],'127.0.0.1')
    def test_token_file_is_owner_only(self):
        tmp=Path(tempfile.mkdtemp());self.addCleanup(shutil.rmtree,tmp,True)
        with patch.object(ui_server,'TOKEN_DIR',tmp/'p'),patch.object(ui_server,'TOKEN_PATH',tmp/'p/ui-token'):
            token=ui_server.load_token();self.assertGreaterEqual(len(token),30)
            self.assertEqual((tmp/'p/ui-token').stat().st_mode & 0o777,0o600)
            self.assertEqual(ui_server.load_token(),token)

if __name__=='__main__':unittest.main()
