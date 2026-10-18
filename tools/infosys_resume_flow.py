"""One controlled Infosys Naukri flow, with guaranteed normal-exit resume restore.

Interactive commands on stdin are limited to: inspect, stop. Opening Apply is
bounded to one flowtype=show request. No screening answer or final submit is
sent by this tool. The browser remains open while commands are read.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from naukri_direct_playwright import ROOT, PROFILE_URL, _chrome, _status
from job_db import connect

JOB_ID=220
REQ='190826038395'
JOB_DIR=ROOT/'jobs/naukri-190826038395'
ROLE=JOB_DIR/'Yugal_Teotia_Java_Backend_Developer.pdf'
GENERIC=ROOT/'master-resume/Yugal_Teotia_CV.pdf'
AUDIT=JOB_DIR/'full-applied-history-audit.json'
EVIDENCE=JOB_DIR/'resume-flow-inspection.json'


def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def sha(path:Path):return hashlib.sha256(path.read_bytes()).hexdigest()


def current_filename(page):
    return page.evaluate("""() => [...document.body.innerText.matchAll(/Yugal_Teotia_[A-Za-z0-9_]+\\.pdf/g)].map(m=>m[0])[0]||null""")


def upload_profile(page,pdf:Path):
    page.goto(PROFILE_URL,wait_until='domcontentloaded',timeout=30000)
    page.wait_for_timeout(1200)
    if page.locator('#attachCV').count()!=1:
        raise RuntimeError('Profile resume control missing')
    before=current_filename(page)
    responses=[]
    def on_response(response):
        parts=urlsplit(response.url)
        if (parts.hostname or '').endswith('naukri.com') and response.request.method in ('POST','PUT','PATCH') and any(x in parts.path.lower() for x in ('resume','upload','cv')):
            responses.append({'endpoint':'profile_resume_upload','status':response.status})
    page.on('response',on_response)
    page.locator('#attachCV').set_input_files(str(pdf))
    page.wait_for_timeout(4500)
    after=None
    for _ in range(4):
        page.reload(wait_until='domcontentloaded',timeout=30000)
        page.wait_for_timeout(1800)
        after=current_filename(page)
        if after==pdf.name:break
        page.wait_for_timeout(3000)
    page.remove_listener('response',on_response)
    if after!=pdf.name:
        raise RuntimeError('Profile did not display the requested resume filename')
    if not any(200<=x['status']<300 for x in responses):
        raise RuntimeError('Profile resume upload lacked a successful server acknowledgement')
    return {'at':now(),'before':before,'after':after,'pdf_sha256':sha(pdf),'response_statuses':responses}


def check_authorization():
    if not ROLE.is_file() or not GENERIC.is_file():
        raise RuntimeError('Role or generic resume PDF is missing')
    audit=json.loads(AUDIT.read_text())
    if (audit.get('requisition_id')!=REQ or not audit.get('full_source_count_loaded')
        or audit.get('final',{}).get('exactIdMatches')!=0
        or audit.get('final',{}).get('companyRoleMatches')!=0):
        raise RuntimeError('Full Applied Jobs absence gate failed')
    db=connect()
    try:
        job=db.execute('SELECT * FROM jobs WHERE id=?',(JOB_ID,)).fetchone()
        attempts=db.execute('SELECT count(*) FROM submission_attempts WHERE job_id=?',(JOB_ID,)).fetchone()[0]
        if not job or job['requisition_id']!=REQ or job['state']!='NeedsReview' or attempts:
            raise RuntimeError('Exact job/state or attempt gate failed')
        return job['canonical_url']
    finally:db.close()


def inspect_chat(page):
    return page.evaluate("""() => {
      const drawer=[...document.querySelectorAll('.chatbot_Drawer')].find(e=>e.getClientRects().length);
      const root=drawer||document;
      const prompts=[...root.querySelectorAll('.botMsg')].filter(e=>e.getClientRects().length).map(e=>e.innerText.trim());
      const yes=root.querySelector('input[type=radio]#Yes');
      const no=root.querySelector('input[type=radio]#No');
      const visibleInputs=[...root.querySelectorAll('input[type=file]')].filter(e=>e.getClientRects().length);
      const names=[...(drawer?.innerText||'').matchAll(/Yugal_Teotia_[A-Za-z0-9_]+\\.pdf/g)].map(m=>m[0]);
      return {drawerVisible:!!drawer,prompts,yesOptionVisible:!!yes&&!!yes.getClientRects().length,
        yesChecked:!!yes?.checked,noChecked:!!no?.checked,
        visibleUploadInputs:visibleInputs.length,visibleResumeNames:names,
        thankYouVisible:/(Thank you for your responses)/i.test(drawer?.innerText||''),
        appliedStatusVisible:/(^|\\n)Applied(\\n|$)/.test(document.body.innerText),
        applyButtonVisible:[...document.querySelectorAll('#apply-button')].some(e=>e.getClientRects().length&&e.innerText.trim()==='Apply')};
    }""")


def main():
    url=check_authorization()
    evidence={'job_id':JOB_ID,'requisition_id':REQ,'url':url,'started_at':now(),
              'role_resume_path':str(ROLE.relative_to(ROOT)),'generic_resume_path':str(GENERIC.relative_to(ROOT)),
              'apply_show_count':0,'chat_answers_sent':0,'final_submit_sent':False}
    with _chrome(headless=False) as context:
        page=context.pages[0] if context.pages else context.new_page()
        if not _status(page)['signed_in']:
            raise RuntimeError('Isolated profile is not signed in')
        restore_needed=False
        guard=None
        try:
            page.goto(PROFILE_URL,wait_until='domcontentloaded',timeout=30000)
            page.wait_for_timeout(1200)
            initial=current_filename(page)
            evidence['initial_profile_filename']=initial
            if initial!=GENERIC.name:
                raise RuntimeError('Generic profile resume was not current; no swap performed')
            restore_needed=True
            evidence['role_upload']=upload_profile(page,ROLE)
            page.goto(url,wait_until='domcontentloaded',timeout=30000)
            page.wait_for_timeout(1300)
            if urlsplit(page.url).path!=urlsplit(url).path or page.locator('#apply-button').count()==0:
                raise RuntimeError('Exact listing or Apply control changed')
            if page.locator('#apply-button').first.inner_text().strip()!='Apply':
                raise RuntimeError('Listing no longer shows unapplied Apply')
            show_response=[]
            blocked=[]
            def on_response(response):
                if urlsplit(response.url).path!='/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply':return
                item={'status':response.status}
                try:
                    packet=response.json()
                    node=packet.get('chatbotResponse',{})
                    serialized=json.dumps(packet,ensure_ascii=False)
                    item.update({'isLeafNode':node.get('isLeafNode'),'dataCommitted':node.get('dataCommitted'),
                                 'isApply':node.get('isApply'),'currentNodeName':node.get('currentNodeName'),
                                 'roleFilenameInResponse':ROLE.name in serialized,
                                 'genericFilenameInResponse':GENERIC.name in serialized,
                                 'prefilledYes':str(node.get('prefilled_value','')).strip().casefold()=='yes',
                                 'prefilledValueType':type(node.get('prefilled_value')).__name__})
                except Exception:item['jsonParsed']=False
                show_response.append(item)
            page.on('response',on_response)
            def guard(route):
                req=route.request
                path=urlsplit(req.url).path
                if req.method in ('GET','HEAD','OPTIONS'):
                    route.continue_();return
                if path=='/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply' and evidence['apply_show_count']==0:
                    try:
                        packet=req.post_data_json
                        if (packet.get('flowtype')=='show' and len(packet.get('strJobsarr',[]))==1
                            and REQ in json.dumps(packet.get('strJobsarr',[]))):
                            evidence['apply_show_count']=1
                            route.continue_();return
                    except Exception:pass
                if any(x in path.lower() for x in ('/apply-workflow/','/botapi/','/upload','/resume','/submit')):
                    blocked.append({'method':req.method,'path':path});route.abort();return
                route.continue_()
            context.route('**/*',guard)
            page.locator('#apply-button').first.click(timeout=10000)
            page.wait_for_timeout(1800)
            evidence['show_response']=show_response
            evidence['chat_initial']=inspect_chat(page)
            evidence['blocked_application_requests']=blocked
            evidence['inspected_at']=now()
            EVIDENCE.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps({'phase':'chat_inspected','evidence_path':str(EVIDENCE.relative_to(ROOT)),
                              'role_upload':evidence['role_upload'],'show_response':show_response,
                              'chat_initial':evidence['chat_initial'],'blocked_application_requests':blocked},ensure_ascii=False),flush=True)
            while True:
                command=input('pilot command (inspect/stop): ').strip().lower()
                if command=='inspect':
                    print(json.dumps({'phase':'chat_reinspection','chat':inspect_chat(page)},ensure_ascii=False),flush=True)
                elif command=='stop':
                    break
                else:
                    print(json.dumps({'error':'unsupported command; no form action taken'}),flush=True)
        finally:
            if restore_needed:
                try:
                    if guard is not None:
                        context.unroute('**/*',guard)
                    evidence['generic_restore']=upload_profile(page,GENERIC)
                    evidence['generic_restored_verified']=evidence['generic_restore']['after']==GENERIC.name
                except Exception as exc:
                    evidence['generic_restored_verified']=False
                    evidence['restore_error_type']=type(exc).__name__
                evidence['finished_at']=now()
                EVIDENCE.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
                print(json.dumps({'phase':'restored','generic_restored_verified':evidence['generic_restored_verified'],
                                  'generic_restore':evidence.get('generic_restore'),
                                  'evidence_path':str(EVIDENCE.relative_to(ROOT))},ensure_ascii=False),flush=True)

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'phase':'stopped','error_type':type(exc).__name__,
                          'error':str(exc) if isinstance(exc,RuntimeError) else 'Browser operation failed'}),flush=True)
        raise SystemExit(1)
