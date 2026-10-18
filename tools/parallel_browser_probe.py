"""Bounded independent MCP connection test; records only hosts and tool status."""
import argparse
import datetime as dt
import json
import os
import re
import select
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from private_playwright import load_private
from tab_lifecycle import OwnedTabs

URLS=('https://www.naukri.com/','https://www.linkedin.com/jobs/','https://www.foundit.in/')

class MCP:
    def __init__(self):
        self.p=subprocess.Popen([sys.executable,str(ROOT/'tools/private_playwright.py')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True,bufsize=0)
        self.id=0;self.buffer=b''
        self.request('initialize',{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'job-search-isolation-probe','version':'1'}},timeout=35)
        self.send({'jsonrpc':'2.0','method':'notifications/initialized'})
    def send(self,msg):
        self.p.stdin.write((json.dumps(msg)+'\n').encode());self.p.stdin.flush()
    def request(self,method,params,timeout=25):
        self.id+=1;identifier=self.id
        self.send({'jsonrpc':'2.0','id':identifier,'method':method,'params':params})
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            ready,_,_=select.select([self.p.stdout],[],[],max(0,end-time.monotonic()))
            if not ready:break
            chunk=os.read(self.p.stdout.fileno(),65536)
            if not chunk:raise RuntimeError('MCP process closed')
            self.buffer+=chunk
            while b'\n' in self.buffer:
                line,self.buffer=self.buffer.split(b'\n',1)
                try:message=json.loads(line)
                except ValueError:continue
                if message.get('id')==identifier:
                    if 'error' in message:raise RuntimeError('MCP request error')
                    return message.get('result',{})
        raise TimeoutError(f'{method} exceeded {timeout}s')
    def tool(self,name,args,timeout=30):
        result=self.request('tools/call',{'name':name,'arguments':args},timeout)
        if result.get('isError'):
            message=' '.join(c.get('text','') for c in result.get('content',[]) if c.get('type')=='text').lower()
            category=next((part for part in ('outside','allowed','chooser','timeout','not found','not visible','not connected','intercept','multiple') if part in message),'other')
            safe=re.sub(r'https?://\S+|/[\w./-]+|[\w.+-]+@[\w.-]+','[redacted]',message)
            raise RuntimeError(f'{name} returned {category} error: {safe[:350]}')
        return result
    def close(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:self.p.wait(timeout=3)
            except subprocess.TimeoutExpired:self.p.kill();self.p.wait()

def run_one(index,url,barrier):
    start=time.monotonic();client=None;owned=None;lifecycle={}
    try:
        client=MCP();owned=OwnedTabs(client, owner=f"probe:{index}")
        # Each connection creates its own tab; no form or account data is read.
        owned.open(url)
        barrier.wait(timeout=50)
        result=client.tool('browser_evaluate',{'function':'() => ({host:location.hostname})'},timeout=30)
        text=' '.join(c.get('text','') for c in result.get('content',[]) if c.get('type')=='text')
        host=next((h for h in ('www.naukri.com','www.linkedin.com','www.foundit.in') if h in text),None)
        expected=url.split('/')[2]
        # Re-check after other connections have interacted with their tabs.
        time.sleep(1)
        again=client.tool('browser_evaluate',{'function':'() => ({host:location.hostname})'},timeout=30)
        again_text=' '.join(c.get('text','') for c in again.get('content',[]) if c.get('type')=='text')
        second=next((h for h in ('www.naukri.com','www.linkedin.com','www.foundit.in') if h in again_text),None)
        return {'worker':index,'expected_host':expected,'observed_host':host,'second_host':second,'isolated':host==expected==second,'seconds':round(time.monotonic()-start,2),'tab_lifecycle':lifecycle}
    except Exception as exc:
        return {'worker':index,'expected_host':url.split('/')[2],'isolated':False,'error':type(exc).__name__+': '+str(exc)[:100],'seconds':round(time.monotonic()-start,2),'tab_lifecycle':lifecycle}
    finally:
        if client:
            if owned:lifecycle.update(owned.close_all())
            client.close()

def form_one(index,url,pdf,barrier):
    started=time.monotonic();client=None;owned=None;lifecycle={}
    try:
        client=MCP();owned=OwnedTabs(client, owner=f"dummy-form:{index}");owned.open(url)
        job=f'TEST-REQ-{index}'
        client.tool('browser_type',{'target':'#job_id','text':job,'submit':False},timeout=25)
        client.tool('browser_type',{'target':'#answer','text':f'confirmed-test-{index}','submit':False},timeout=25)
        barrier.wait(timeout=45)
        observation='() => ({url:location.href,job:document.querySelector("#job_id")?.value||"",answer:document.querySelector("#answer")?.value||"",resume:document.querySelector("#resume")?.files[0]?.name||"",submitted:window.submitted||false})'
        result=client.tool('browser_evaluate',{'function':observation},timeout=25)
        text=' '.join(c.get('text','') for c in result.get('content',[]) if c.get('type')=='text')
        checks=[url in text,job in text,f'confirmed-test-{index}' in text,'"submitted": false' in text or '"submitted":false' in text]
        wrong=f'TEST-REQ-{3-index}' in text or f'confirmed-test-{3-index}' in text
        fields_ok=all(checks) and not wrong
        upload_error=None;resume_ok=False;click_error=None
        try:
            client.tool('browser_click',{'target':'#resume'},timeout=25)
        except Exception as exc:click_error=str(exc)[:250]
        try:
            client.tool('browser_file_upload',{'paths':[str(pdf)]},timeout=25)
            verified=client.tool('browser_evaluate',{'function':observation},timeout=25)
            check=' '.join(c.get('text','') for c in verified.get('content',[]) if c.get('type')=='text')
            resume_ok=pdf.name in check and f'Test_Resume_{3-index}.pdf' not in check
        except Exception as exc:upload_error=str(exc)[:350]
        final_error=None
        try:
            final=client.tool('browser_evaluate',{'function':observation},timeout=25)
            final_text=' '.join(c.get('text','') for c in final.get('content',[]) if c.get('type')=='text')
            observed=next((json.JSONDecoder().raw_decode(final_text[pos:])[0] for pos,ch in enumerate(final_text) if ch=='{' and _json_object(final_text[pos:])),None) or {}
        except Exception as exc:
            observed={};final_error=type(exc).__name__+': '+str(exc)[:250]
        return {'worker':index,'job_id':job,'expected_url':url,'expected_resume':pdf.name,
                'observed':{k:observed.get(k) for k in ('url','job','resume','submitted')},'fields_isolated':fields_ok,
                'resume_isolated':resume_ok,'isolated':fields_ok and resume_ok,'click_error':click_error,
                'upload_error':upload_error,'final_read_error':final_error,
                'seconds':round(time.monotonic()-started,2),'field_checks':checks,'tab_lifecycle':lifecycle}
    except Exception as exc:
        return {'worker':index,'isolated':False,'error':type(exc).__name__+': '+str(exc)[:400],'seconds':round(time.monotonic()-started,2),'tab_lifecycle':lifecycle}
    finally:
        if client:
            if owned:lifecycle.update(owned.close_all())
            client.close()

def _json_object(value):
    try:return isinstance(json.JSONDecoder().raw_decode(value)[0],dict)
    except ValueError:return False

class FormHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/form/1','/form/2'):
            self.send_error(404);return
        html=b'<html><body><form onsubmit="window.submitted=true;return false"><input id="job_id"><input id="answer"><input id="resume" type="file"><button type="submit">Submit</button></form></body></html>'
        self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(html)));self.end_headers();self.wfile.write(html)
    def log_message(self,*args):pass

def forms_test(single=False,sequential=False):
    import threading
    server=ThreadingHTTPServer(('127.0.0.1',0),FormHandler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        fixture_root=ROOT/'.workflow'/'test-fixtures'
        fixture_root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='forms-',dir=fixture_root) as folder:
            paths=[]
            for index in (1,2):
                pdf=Path(folder)/f'Test_Resume_{index}.pdf'
                pdf.write_bytes(b'%PDF-1.4\n% Isolated upload test fixture; never employer-facing.\n')
                paths.append(pdf)
            count=1 if single else 2
            if sequential:
                rows=[form_one(i,f'http://127.0.0.1:{server.server_port}/form/{i}',paths[i-1],threading.Barrier(1)) for i in range(1,count+1)]
            else:
                barrier=threading.Barrier(count)
                with ThreadPoolExecutor(max_workers=count) as pool:
                    futures=[pool.submit(form_one,i,f'http://127.0.0.1:{server.server_port}/form/{i}',paths[i-1],barrier) for i in range(1,count+1)]
                    rows=[f.result(timeout=110) for f in futures]
            return {'test':f'{count} local form preparation(s), no submission','sequential':sequential,'flows':rows,'passed':all(r['isolated'] for r in rows)}
    finally:server.shutdown();server.server_close()

def main():
    load_private()
    def emit(result,kind):
        path=ROOT/'.workflow'/'parallel-tests'/(dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+kind+'.json')
        path.parent.mkdir(parents=True,exist_ok=True)
        result['evidence']=str(path.relative_to(ROOT))
        path.write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result,indent=2))
    if '--single-form' in sys.argv:
        emit(forms_test(single=True),'single-form');return
    if '--forms' in sys.argv:
        emit(forms_test(),'two-forms');return
    if '--forms-sequential' in sys.argv:
        emit(forms_test(sequential=True),'two-forms-sequential');return
    import threading
    barrier=threading.Barrier(3)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(run_one,i,u,barrier) for i,u in enumerate(URLS,1)]
        rows=[f.result(timeout=100) for f in futures]
    emit({'test':'three independent MCP connections, dedicated read-only tabs','flows':rows,'passed':all(r['isolated'] for r in rows)},'three-read-only')

if __name__=='__main__':main()
