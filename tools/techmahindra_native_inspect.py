"""One guarded read/inspect pass for the explicitly authorized Naukri pilot."""
import datetime as dt
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0,'tools')
from naukri_direct_playwright import ROOT,_chrome,_status
from infosys_resume_flow import current_filename,upload_profile,sha
from naukri_phase_guard import NaukriPhaseGuard,APPLY_PATH,CHAT_PATH
from job_db import connect,transition
from job_views import regenerate

JOB_ID=244;REQ='070926003879'
FOLDER=ROOT/'jobs/naukri-070926003879'
ROLE=FOLDER/'Yugal_Teotia_Java_Backend_Developer.pdf'
GENERIC=ROOT/'master-resume/Yugal_Teotia_CV.pdf'
URL='https://www.naukri.com/job-listings-java-backend-developer-naukri-e-hire-campaign-mumbai-1-to-4-years-070926003879'
PREFLIGHT=FOLDER/'preflight-20260929.json'
OUT=FOLDER/'naukri-native-inspection.json'

def now():return dt.datetime.now(dt.timezone.utc).isoformat()

def listing_state(page):
    page.goto(URL,wait_until='domcontentloaded',timeout=30000);page.wait_for_timeout(1200)
    return page.evaluate('''req=>{const b=[...document.querySelectorAll('#apply-button')].filter(x=>x.getClientRects().length);const t=document.body.innerText;
      return {exact_path:location.pathname.endsWith(req),title:document.title,
        apply_visible:b.some(x=>x.innerText.trim()==='Apply'),applied_button:b.some(x=>/Applied/i.test(x.innerText)),
        applied_status:/(^|\\n)Applied(\\n|$)/.test(t),pending:/Continue application|Pending application/i.test(t)};}''',REQ)

def chat_state(page):
    return page.evaluate('''()=>{const d=[...document.querySelectorAll('.chatbot_Drawer')].find(x=>x.getClientRects().length);const root=d||document;
      const visible=x=>!!x&&!!x.getClientRects().length;
      const prompts=[...root.querySelectorAll('.botMsg')].filter(visible).map(x=>x.innerText.trim());
      const fields=[...root.querySelectorAll('input:not([type=hidden]),textarea,[contenteditable=true],select')].filter(visible).map((x,i)=>({index:i,type:x.tagName==='SELECT'?'select':x.getAttribute('type')||x.tagName.toLowerCase(),
        required:!!x.required||x.getAttribute('aria-required')==='true',filled:!!(x.value||x.innerText||'').trim(),label:(x.labels&&[...x.labels].map(y=>y.innerText.trim()).join(' '))||x.getAttribute('aria-label')||''}));
      const radios=[...root.querySelectorAll('input[type=radio]')].filter(visible).map(x=>({name:x.name,label:(x.labels&&[...x.labels].map(y=>y.innerText.trim()).join(' '))||x.getAttribute('aria-label')||'',checked:x.checked}));
      const saves=[...root.querySelectorAll('.sendMsgbtn_container')].filter(visible).map(x=>({label:x.innerText.trim(),disabled:x.classList.contains('disabled')||!!x.querySelector('[disabled]'),class:x.className}));
      const nodes=d?[...d.querySelectorAll('.botMsg,.sendMsgbtn_container,[contenteditable=true],input,textarea,select')].filter(visible).map(x=>({tag:x.tagName,cls:String(x.className||''),text:(x.innerText||'').trim().slice(0,180),placeholder:x.getAttribute('placeholder')||'',required:!!x.required,aria:x.getAttribute('aria-label')||''})):[];
      return {drawer_visible:!!d,prompts,fields,radio_options:radios,save_controls:saves,nodes,thank_you:/Thank you for your responses/i.test(d?.innerText||''),visible_resume_names:[...(d?.innerText||'').matchAll(/Yugal_Teotia_[A-Za-z0-9_]+\\.pdf/g)].map(x=>x[0])};}''')

def main():
    db=connect()
    try:
        job=db.execute('select * from jobs where id=?',(JOB_ID,)).fetchone()
        if not job or job['requisition_id']!=REQ or job['canonical_url']!=URL or job['source']!='naukri_portal_native' or job['fit']!='Medium' or job['state'] not in ('ResumeReady','InProgress'):raise RuntimeError('Exact staged job/state gate failed')
        if db.execute('select 1 from submission_attempts where job_id=?',(JOB_ID,)).fetchone():raise RuntimeError('Existing attempt blocks job')
        if db.execute('select 1 from history_keys where employer_key=? and requisition_key=?',(job['employer_key'],job['requisition_key'])).fetchone():raise RuntimeError('History key blocks job')
    finally:db.close()
    pre=json.loads(PREFLIGHT.read_text())
    if (pre.get('requisition_id')!=REQ or not pre.get('history',{}).get('full_history_loaded') or
        pre['history'].get('exact_id_matches') or pre['history'].get('company_role_matches') or
        not pre.get('listing',{}).get('apply_visible')):raise RuntimeError('Fresh complete history/listing preflight missing')
    age=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(pre['listing_checked_at'].replace('Z','+00:00'))).total_seconds()
    if age<0 or age>900:raise RuntimeError('Preflight is stale')
    if not ROLE.is_file() or not GENERIC.is_file():raise RuntimeError('Role or generic resume missing')
    visual=json.loads((FOLDER/'resume_visual_review.json').read_text())
    if visual.get('status')!='visually_clean' or visual.get('pdf_sha256')!=sha(ROLE):raise RuntimeError('Role PDF visual review/hash mismatch')
    result={'job_id':JOB_ID,'requisition_id':REQ,'url':URL,'started_at':now(),'attempt_id':None,
        'submission_attempted':False,'final_submit_clicked':False,'form_opened':False,'chat':{},'network_guard':None}
    with _chrome(headless=False) as context:
        page=context.pages[0] if context.pages else context.new_page()
        if not _status(page)['signed_in']:raise RuntimeError('Isolated Naukri account signed out')
        if not page.evaluate("()=>/Yugal\\s+Teotia/i.test(document.body.innerText)"):raise RuntimeError('Signed-in account identity mismatch')
        if current_filename(page)!=GENERIC.name:raise RuntimeError('Generic Naukri profile resume is not current; no file changed')
        restore=True;result['signed_in']=True;result['account_name_matches']=True
        result['profile_before']=current_filename(page)
        try:
            result['role_upload']=upload_profile(page,ROLE)
            if result['role_upload']['after']!=ROLE.name:raise RuntimeError('Role-specific profile resume not verified')
            result['profile_verified_at']=result['role_upload']['at']
            state=listing_state(page);result['listing_before']=state;result['listing_checked_at']=now()
            if not state['exact_path'] or not state['apply_visible'] or state['applied_button'] or state['applied_status'] or state['pending']:
                raise RuntimeError('Exact listing no longer live and unapplied')
            phase=NaukriPhaseGuard(REQ);pending=[];responses=[]
            def on_response(response):
                path=urlsplit(response.url).path
                if path not in (APPLY_PATH,CHAT_PATH):return
                ev=next((x for x in pending if x['path']==path and x['response_status'] is None),None)
                item={'path':path,'status':response.status};metadata={}
                try:
                    body=response.json();node=body.get('chatbotResponse',{}) if isinstance(body,dict) else {}
                    metadata={'isApply':node.get('isApply') if isinstance(node.get('isApply'),bool) else None,
                        'isLeafNode':node.get('isLeafNode') if isinstance(node.get('isLeafNode'),bool) else None,
                        'dataCommitted':node.get('dataCommitted') if isinstance(node.get('dataCommitted'),bool) else None,
                        'job_id_match':REQ in json.dumps(body,ensure_ascii=False),
                        'currentNodeName':node.get('currentNodeName') if isinstance(node.get('currentNodeName'),str) else None}
                except Exception:pass
                phase.response(ev,response.status,metadata)
                item.update({'flowtype':ev['flowtype'] if ev else 'unknown','reason':ev['reason'] if ev else 'unmatched_response',
                             'response_metadata':{k:v for k,v in metadata.items() if v is not None}})
                responses.append(item)
            page.on('response',on_response)
            def intercept(route):
                req=route.request;path=urlsplit(req.url).path
                if req.method in ('GET','HEAD','OPTIONS'):route.continue_();return
                try:packet=req.post_data_json if path in (APPLY_PATH,CHAT_PATH) else None
                except Exception:packet=None
                allowed,event=phase.decide(req.method,req.url,packet)
                if event:pending.append(event)
                if allowed:route.continue_()
                else:route.abort()
            context.route('**/*',intercept)
            button=page.locator('#apply-button:visible').filter(has_text='Apply')
            if button.count()!=1:raise RuntimeError('Exact Apply action unavailable')
            button.click(timeout=10000);page.wait_for_timeout(5200)
            result['form_opened']=True;result['chat']=chat_state(page)
            result['network_responses']=responses;result['network_guard']=phase.summary()
            if phase.phase!='chat_ready' or phase.show_count!=1 or not result['chat']['drawer_visible']:
                raise RuntimeError('Exact job-bound Apply show did not open questionnaire')
            if result['chat']['thank_you']:raise RuntimeError('Unexpected prior completed chat; stop for outcome investigation')
            result['inspected_at']=now()
            OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
            if job['state']=='ResumeReady':
                db=connect()
                try:transition(db,JOB_ID,'InProgress','techmahindra_native_pilot','Exact job-bound native Apply questionnaire opened; no answer/save sent.',str(OUT.relative_to(ROOT)),'ResumeReady')
                finally:db.close()
            context.unroute('**/*',intercept)
            result['inspection_only']=True
        finally:
            if restore:
                result['generic_restore']=upload_profile(page,GENERIC)
                result['generic_restored_verified']=current_filename(page)==GENERIC.name
                if not result['generic_restored_verified']:raise RuntimeError('Generic Naukri profile resume restore failed')
            result['finished_at']=now();OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    regenerate()
    print(json.dumps({'job_id':JOB_ID,'requisition_id':REQ,'phase':phase.phase,
        'question_count':len(result['chat']['prompts']),'questions':result['chat']['prompts'],
        'fields':result['chat']['fields'],'radio_options':result['chat']['radio_options'],
        'save_labels':result['chat']['save_labels'],'network_guard':result['network_guard'],
        'resume_restored':result['generic_restored_verified'],'submission_attempted':False,
        'evidence_path':str(OUT.relative_to(ROOT))},ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except Exception as e:
        print(json.dumps({'error_type':type(e).__name__,'error':str(e) if isinstance(e,RuntimeError) else 'Inspection failed'}))
        raise SystemExit(1)
