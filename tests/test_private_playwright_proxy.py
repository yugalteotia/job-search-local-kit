import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / 'tools' / 'private_playwright_proxy.py'


class ProxyTests(unittest.TestCase):
    def test_proxy_reuses_owned_tab_and_blocks_unowned_close(self):
        with tempfile.TemporaryDirectory(prefix='fake-playwright-') as folder:
            temp = Path(folder)
            events = temp / 'upstream.jsonl'
            fake = temp / 'npx'
            fake.write_text('''#!/usr/bin/env python3
import json,os,socket,sys
from urllib.parse import urlencode
listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen()
port=listener.getsockname()[1]
next_id=1
for line in sys.stdin:
 try: request=json.loads(line)
 except ValueError: continue
 if 'id' not in request: continue
 params=request.get('params',{}); name=params.get('name'); args=params.get('arguments',{}) or {}; action=args.get('action')
 if request.get('method')=='initialize': result={'protocolVersion':'2025-03-26','capabilities':{},'serverInfo':{'name':'fake','version':'1'}}
 elif request.get('method')=='tools/call':
  with open(os.environ['FAKE_EVENTS'],'a') as out: out.write(json.dumps({'name':name,'action':action,'index':args.get('index'),'url':args.get('url')})+'\\n')
  if name=='browser_tabs' and action=='list':
   href='chrome-extension://test-extension/connect.html?'+urlencode({'mcpRelayUrl':f'ws://127.0.0.1:{port}/extension/connection','client':json.dumps({'name':'test'}),'protocolVersion':'2','token':'FAKE_SECRET_SENTINEL'})
   result={'content':[{'type':'text','text':f'- 0: (current) [Welcome]({href})'}]}
  elif name=='browser_tabs' and action=='new':
   result={'content':[{'type':'text','text':f'- {next_id}: (current) [owned](https://example.invalid/)\\n- 0: [Welcome](chrome-extension://test-extension/connect.html?token=FAKE_SECRET_SENTINEL)'}]};next_id+=1
  elif name=='browser_evaluate':
   result={'content':[{'type':'text','text':'### Result\\n{"ok":true}\\n### Open tabs\\n- 0: [Welcome](chrome-extension://test-extension/connect.html?token=FAKE_SECRET_SENTINEL)'}]}
  else: result={'content':[{'type':'text','text':'ok'}]}
 else: result={}
 print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
''')
            fake.chmod(0o700)
            run_id = 'proxy-test-' + str(os.getpid())
            env = {**os.environ, 'PATH': folder + os.pathsep + os.environ.get('PATH', ''),
                   'FAKE_EVENTS': str(events), 'JOB_SEARCH_RUN_ID': run_id}
            proc = subprocess.Popen([sys.executable, str(PROXY)], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

            def call(identifier, method, params):
                proc.stdin.write(json.dumps({'jsonrpc':'2.0','id':identifier,'method':method,'params':params})+'\n')
                proc.stdin.flush()
                ready, _, _ = select.select([proc.stdout], [], [], 5)
                if not ready: self.fail('proxy response timed out')
                while True:
                    message=json.loads(proc.stdout.readline())
                    if message.get('id')==identifier:return message
                    ready, _, _ = select.select([proc.stdout], [], [], 5)
                    if not ready:self.fail('proxy response timed out')

            try:
                call(1,'initialize',{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'test','version':'1'}})
                proc.stdin.write(json.dumps({'jsonrpc':'2.0','method':'notifications/initialized'})+'\n');proc.stdin.flush()
                created=call(2,'tools/call',{'name':'browser_tabs','arguments':{'action':'new','url':'https://example.invalid/one'}})
                self.assertIn('1: (current)',created['result']['content'][0]['text'])
                self.assertNotIn('FAKE_SECRET_SENTINEL',json.dumps(created))
                self.assertNotIn('chrome-extension://',json.dumps(created))
                evaluated=call(5,'tools/call',{'name':'browser_evaluate','arguments':{'function':'() => ({ok:true})'}})
                self.assertIn('"ok":true',evaluated['result']['content'][0]['text'])
                self.assertNotIn('FAKE_SECRET_SENTINEL',json.dumps(evaluated))
                self.assertNotIn('chrome-extension://',json.dumps(evaluated))
                listed=call(6,'tools/call',{'name':'browser_tabs','arguments':{'action':'list'}})
                self.assertNotIn('FAKE_SECRET_SENTINEL',json.dumps(listed))
                self.assertNotIn('chrome-extension://',json.dumps(listed))
                reused=call(3,'tools/call',{'name':'browser_tabs','arguments':{'action':'new','url':'https://example.invalid/two'}})
                self.assertFalse(reused['result'].get('isError',False))
                denied=call(4,'tools/call',{'name':'browser_tabs','arguments':{'action':'close','index':0}})
                self.assertTrue(denied['result']['isError'])
            finally:
                proc.stdin.close();proc.wait(timeout=10);proc.stdout.close();proc.stderr.close()
            upstream=[json.loads(line) for line in events.read_text().splitlines()]
            self.assertEqual([(x['action'],x['index']) for x in upstream if x['name']=='browser_tabs'],
                             [('list',None),('new',None),('list',None),('close',1),('close',0)])
            self.assertIn(('browser_navigate','two'),[(x['name'],x['url'].rsplit('/',1)[-1]) for x in upstream if x['name']=='browser_navigate'])
            log=ROOT/'.workflow'/'runs'/f'{run_id}-tabs.jsonl'
            records=[json.loads(line) for line in log.read_text().splitlines()]
            self.assertNotIn('FAKE_SECRET_SENTINEL',log.read_text())
            summary=next(x for x in records if x['event']=='tab_lifecycle_summary')
            self.assertEqual((summary['created'],summary['reused'],summary['closed'],summary['remaining_automation_tabs']),(2,1,2,0))
            self.assertEqual((summary['job_tabs_created'],summary['welcome_tabs_created'],summary['welcome_tabs_closed']),(1,1,1))
            self.assertTrue(summary['automatic_cleanup'])
            self.assertTrue(any(x['event']=='welcome_tab_created' and x['tab_id']==0 for x in records))
            self.assertTrue(any(x['event']=='welcome_tab_closed' and x['tab_id']==0 for x in records))


if __name__ == '__main__':
    unittest.main()
