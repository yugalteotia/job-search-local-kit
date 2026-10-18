"""One claimed Agiliad Naukri native-Apply attempt in the isolated Chrome profile."""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from infosys_resume_flow import current_filename, now, sha, upload_profile
from naukri_direct_playwright import ROOT, _chrome, _status
from naukri_phase_guard import NaukriPhaseGuard, APPLY_PATH, CHAT_PATH
from job_db import begin_attempt, confirm_attempt, connect, record_evidence, transition
from job_views import regenerate

JOB_ID=241
REQ='280926023134'
URL='https://www.naukri.com/job-listings-java-developer-senior-java-developer-agiliad-pune-2-to-6-years-280926023134'
QUESTION='How many years of experience do you have in Backend?'
FOLDER=ROOT/'jobs/agiliad-java-backend-developer-280926023134'
ROLE=FOLDER/'Yugal_Teotia_Java_Backend_Developer.pdf'
GENERIC=ROOT/'master-resume/Yugal_Teotia_CV.pdf'
PRESUBMIT=FOLDER/'naukri-native-presubmit.json'
OUTCOME=FOLDER/'naukri-native-attempt-outcome.json'
CONFIRMATION=FOLDER/'naukri-native-confirmation.json'


def years_answer():
    """Floor completed months since confirmed IDeaS joining; never round up."""
    today=dt.datetime.now(ZoneInfo('Asia/Kolkata')).date()
    start=dt.date(2024,10,14)
    months=(today.year-start.year)*12+today.month-start.month-(today.day<start.day)
    if months<0:raise RuntimeError('Confirmed joining date is in the future')
    years,remainder=divmod(months,12)
    return f'{years} year'+('' if years==1 else 's')+f' {remainder} month'+('' if remainder==1 else 's')


def history(page):
    page.goto('https://www.naukri.com/myapply/historypage',wait_until='domcontentloaded',timeout=30000)
    page.wait_for_timeout(1200)
    source=page.locator('.apply-filters li').filter(has_text='Applies on Naukri')
    if source.count()!=1:raise RuntimeError('Applied Jobs source filter unavailable')
    source.click();page.wait_for_timeout(1100)
    counts=[];matched=0;target=None
    for _ in range(15):
        data=page.evaluate('''req=>{const rows=[...document.querySelectorAll('.jdTuplesContainer .jdTupleContainer')];
          const labels=[...document.querySelectorAll('.apply-filters li')].map(x=>x.innerText.trim());
          const more=document.querySelector('#loadMoreLnk');
          return {loaded:rows.length,exact:rows.filter(x=>x.outerHTML.includes(req)).length,
                  labels,more:!!more&&!!more.getClientRects().length};}''',REQ)
        counts.append(data['loaded']);matched=data['exact']
        match=re.search(r'Applies on Naukri\s*\((\d+)\)',' '.join(data['labels']))
        target=int(match.group(1)) if match else None
        if matched or (target is not None and data['loaded']>=target) or not data['more']:break
        more=page.locator('#loadMoreLnk')
        more.scroll_into_view_if_needed(timeout=5000)
        more.click(timeout=5000,force=True)
        page.wait_for_timeout(1000)
    return {'checked_at':now(),'url':'https://www.naukri.com/myapply/historypage',
            'loaded_rows':counts[-1],'source_total':target,'full_source_loaded':target is not None and counts[-1]>=target,
            'exact_id_matches':matched,'sweeps':counts}


def listing(page):
    page.goto(URL,wait_until='domcontentloaded',timeout=30000)
    page.wait_for_timeout(1200)
    return page.evaluate('''req=>{const buttons=[...document.querySelectorAll('#apply-button')].filter(x=>x.getClientRects().length);
      const text=document.body.innerText;return {exact:location.pathname.endsWith(req),
        apply:buttons.some(x=>x.innerText.trim()==='Apply'),
        applied:buttons.some(x=>/Applied/i.test(x.innerText))||/(^|\\n)Applied(\\n|$)/.test(text),
        pending:/Continue application|Pending application/i.test(text)};}''',REQ)


def write(data):OUTCOME.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')


def preflight():
    if not ROLE.is_file() or not GENERIC.is_file():raise RuntimeError('Role or generic PDF missing')
    review=json.loads((FOLDER/'resume_visual_review.json').read_text())
    if review.get('status')!='visually_clean' or review.get('pdf_sha256')!=sha(ROLE):
        raise RuntimeError('Exact role PDF visual hash mismatch')
    db=connect()
    try:
        row=db.execute('SELECT * FROM jobs WHERE id=?',(JOB_ID,)).fetchone()
        if (not row or row['requisition_id']!=REQ or row['canonical_url']!=URL or
            row['source']!='naukri_portal_native' or row['state']!='Verified' or row['fit']!='Medium'):
            raise RuntimeError('Exact new Naukri job is not in eligible pre-attempt state')
        if db.execute('SELECT 1 FROM submission_attempts WHERE job_id=?',(JOB_ID,)).fetchone():
            raise RuntimeError('Existing attempt blocks another application')
        if db.execute('SELECT 1 FROM history_keys WHERE employer_key=? AND requisition_key=?',
                      (row['employer_key'],row['requisition_key'])).fetchone():
            raise RuntimeError('History key blocks another application')
    finally:db.close()


def main():
    preflight()
    evidence={'job_id':JOB_ID,'requisition_id':REQ,'url':URL,'started_at':now(),
              'attempt_id':None,'save_click_count':0,'outcome':'Unstarted',
              'resume_sha256':sha(ROLE),'submission_enabled_for_schedule':False}
    restore=False;handler=None;attempt_id=None
    with _chrome(headless=False) as context:
        page=context.pages[0] if context.pages else context.new_page()
        try:
            if not _status(page)['signed_in']:raise RuntimeError('Isolated Naukri session signed out')
            if not page.evaluate("() => /Yugal\\s+Teotia/i.test(document.body.innerText)"):
                raise RuntimeError('Signed-in account identity mismatch')
            if current_filename(page)!=GENERIC.name:
                raise RuntimeError('Generic resume not current at pilot start')
            restore=True
            before=history(page)
            evidence['history_before']=before
            if not before['full_source_loaded'] or before['exact_id_matches']:
                raise RuntimeError('Full prior application history is not clear')
            evidence['role_upload']=upload_profile(page,ROLE)
            if evidence['role_upload']['after']!=ROLE.name:
                raise RuntimeError('Role PDF not displayed on profile')
            state=listing(page);evidence['listing_before']=state
            if not state['exact'] or not state['apply'] or state['applied'] or state['pending']:
                raise RuntimeError('Exact listing is not live and unapplied')
            evidence['listing_checked_at']=now()
            guard=NaukriPhaseGuard(REQ)
            responses=[]
            expected=years_answer()
            def on_response(response):
                path=urlsplit(response.url).path
                if path not in (APPLY_PATH,CHAT_PATH):return
                event=next((e for e in guard.events if e['path']==path and e['allowed'] and e['response_status'] is None),None)
                item={'path':path,'status':response.status,'job_id_match':False,
                      'is_apply':None,'data_committed':None}
                try:
                    body=response.json()
                    serialized=json.dumps(body,ensure_ascii=False)
                    node=body.get('chatbotResponse',{}) if isinstance(body,dict) else {}
                    item['job_id_match']=REQ in serialized
                    item['is_apply']=node.get('isApply') if isinstance(node.get('isApply'),bool) else None
                    item['data_committed']=node.get('dataCommitted') if isinstance(node.get('dataCommitted'),bool) else None
                    item['is_leaf_node']=node.get('isLeafNode') if isinstance(node.get('isLeafNode'),bool) else None
                    item['current_node_name']=node.get('currentNodeName') if isinstance(node.get('currentNodeName'),str) else None
                except Exception:pass
                guard.response(event,response.status,{
                    'isApply':item['is_apply'],'dataCommitted':item['data_committed'],
                    'isLeafNode':item['is_leaf_node'],'currentNodeName':item['current_node_name'],
                    'job_id_match':item['job_id_match']})
                responses.append(item)
            page.on('response',on_response)
            def intercept(route):
                request=route.request;path=urlsplit(request.url).path
                try:payload=request.post_data_json if path in (APPLY_PATH,CHAT_PATH) else None
                except Exception:payload=None
                if path==CHAT_PATH:
                    value=payload.get('input',{}).get('text') if isinstance(payload,dict) and isinstance(payload.get('input'),dict) else None
                    if not (value==[expected] or value==expected):
                        guard._event(path,request.method,None,False,'answer_not_confirmed')
                        route.abort();return
                allowed,_=guard.decide(request.method,request.url,payload)
                if allowed:route.continue_()
                else:route.abort()
            handler=intercept;context.route('**/*',intercept)
            page.locator('#apply-button').first.click(timeout=10000)
            page.wait_for_timeout(1700)
            chat=page.evaluate('''() => {const d=[...document.querySelectorAll('.chatbot_Drawer')].find(x=>x.getClientRects().length);
              return {drawer:!!d,questions:[...(d?.querySelectorAll('.botMsg')||[])].filter(x=>x.getClientRects().length).map(x=>x.innerText.trim()),
                editables:[...(d?.querySelectorAll('[contenteditable=true]')||[])].filter(x=>x.getClientRects().length).length,
                prefilled:[...(d?.querySelectorAll('[contenteditable=true]')||[])].filter(x=>x.getClientRects().length).some(x=>x.innerText.trim()),
                upload_inputs:[...(d?.querySelectorAll('input[type=file]')||[])].filter(x=>x.getClientRects().length).length,
                saved:/(Thank you for your responses)/i.test(d?.innerText||'')};}''')
            evidence['chat_ui']={'drawer':chat['drawer'],'question_count':len(chat['questions']),
                                 'editables':chat['editables'],'prefilled':chat['prefilled'],
                                 'upload_inputs':chat['upload_inputs'],'saved':chat['saved']}
            if (guard.phase!='chat_ready' or chat['questions']!=[QUESTION] or
                chat['editables']!=1 or chat['prefilled'] or chat['saved']):
                raise RuntimeError('Expected unanswered backend-experience question is unavailable')
            field=page.locator('.chatbot_Drawer [contenteditable=true]')
            field.fill(expected,timeout=5000)
            if field.inner_text().strip()!=expected:
                raise RuntimeError('Truthful experience answer did not remain in the editor')
            save=page.locator('.chatbot_Drawer .sendMsgbtn_container')
            if save.count()!=1 or save.inner_text().strip()!='Save':
                raise RuntimeError('Potentially submitting Save is not identified')
            packet={'job_id':JOB_ID,'portal_job_id':REQ,'form_url':URL,'provider':'naukri_native_chat',
                    'signed_in':True,'account_name_matches':True,'exact_listing_id':REQ,
                    'profile_resume_filename':ROLE.name,'profile_resume_sha256':sha(ROLE),
                    'profile_verified_at':evidence['role_upload']['at'],
                    'listing_checked_at':evidence['listing_checked_at'],
                    'history_checked_at':before['checked_at'],'screening_question':QUESTION,
                    'screening_answer_confirmed':True,'answer_policy':'completed_months_from_confirmed_IDeaS_start',
                    'potential_submit_action':'screening_chat_save','per_application_picker_visible':False,
                    'checks':{key:True for key in ('listing_live','not_previously_applied','jd_reviewed',
                        'resume_visually_reviewed','exact_resume_selected','answers_confirmed','final_submit_identified')},
                    'recorded_at':now()}
            PRESUBMIT.write_text(json.dumps(packet,ensure_ascii=False,indent=2)+'\n')
            db=connect()
            try:
                resume_path=str(ROLE.relative_to(ROOT));form_path=str(PRESUBMIT.relative_to(ROOT))
                resume_sha=record_evidence(db,JOB_ID,'resume_review',resume_path)
                form_sha=record_evidence(db,JOB_ID,'form_review',form_path)
                transition(db,JOB_ID,'InProgress','agiliad_native_pilot','Exact native Apply chat opened',form_path,'Verified')
                transition(db,JOB_ID,'Preparing','agiliad_native_pilot','Confirmed experience answer entered truthfully',form_path,'InProgress')
                transition(db,JOB_ID,'Prepared','agiliad_native_pilot','Role PDF verified and Save identified as potentially submitting',form_path,'Preparing')
                attempt_id=begin_attempt(db,JOB_ID,resume_path,resume_sha,URL,form_path,form_sha,pilot=True)
                guard.claim(attempt_id)
            finally:db.close()
            evidence['attempt_id']=attempt_id;evidence['claimed_at']=now();write(evidence)
            guard.arm_save(action_label=save.inner_text().strip(),exact_chat=chat['drawer'],
                           confirmed_answer=field.inner_text().strip()==expected)
            evidence['save_click_count']=1
            save.click(timeout=10000)
            page.wait_for_timeout(2500)
            evidence['network_guard']=guard.summary()
            evidence['network_responses']=responses
            evidence['chat_after_save']=page.evaluate('''() => {const d=document.querySelector('.chatbot_Drawer');
                return {thank_you:/(Thank you for your responses)/i.test(d?.innerText||''),
                  visible_question_count:[...(d?.querySelectorAll('.botMsg')||[])].filter(x=>x.getClientRects().length).length};}''')
            evidence['save_finished_at']=now();write(evidence)
            completion=[e for e in guard.events if e['reason']=='claimed_save_completion']
            completed_network=(len(completion)==1 and isinstance(completion[0]['response_status'],int)
                               and 200<=completion[0]['response_status']<300)
            evidence['completion_network_2xx']=completed_network
            after=listing(page);evidence['listing_after']=after
            after_history=history(page);evidence['history_after']=after_history
            positive=(after['exact'] and after['applied']) or (after_history.get('full_source_loaded') is True and after_history['exact_id_matches']>0)
            if completed_network and positive:
                captured=now()
                proof={'attempt_id':attempt_id,'requisition_id':REQ,'captured_at':captured,'url':URL,
                       'confirmation_text':'Applied on exact listing' if after['applied'] else 'Exact job ID in Naukri Applied Jobs',
                       'completion_flowtype':completion[0]['flowtype'],
                       'completion_http_status':completion[0]['response_status'],
                       'exact_listing_applied':after['applied'],
                       'exact_history_id_match':after_history.get('full_source_loaded') is True and after_history['exact_id_matches']>0,
                       'history_full':after_history.get('full_source_loaded') is True}
                CONFIRMATION.write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n')
                db=connect()
                try:confirm_attempt(db,attempt_id,JOB_ID,str(CONFIRMATION.relative_to(ROOT)),captured,pilot=True)
                finally:db.close()
                evidence['outcome']='Applied with exact job-bound proof'
                evidence['confirmation_path']=str(CONFIRMATION.relative_to(ROOT))
            else:
                evidence['outcome']='Uncertain; one Save clicked, no retry permitted'
            evidence['outcome_checked_at']=now();write(evidence)
        except Exception as exc:
            evidence['error_type']=type(exc).__name__
            evidence['outcome']='Uncertain; no retry' if attempt_id else 'Stopped before submitting action'
            evidence['error_at']=now();write(evidence)
        finally:
            if handler is not None:context.unroute('**/*',handler)
            if restore:
                try:
                    evidence['generic_restore']=upload_profile(page,GENERIC)
                    evidence['generic_restored_verified']=current_filename(page)==GENERIC.name
                except Exception as exc:
                    evidence['generic_restored_verified']=False
                    evidence['restore_error_type']=type(exc).__name__
            evidence['finished_at']=now();write(evidence)
    regenerate()
    print(json.dumps({'job_id':JOB_ID,'requisition_id':REQ,'attempt_id':attempt_id,
                      'outcome':evidence['outcome'],'save_click_count':evidence['save_click_count'],
                      'completion_network_2xx':evidence.get('completion_network_2xx'),
                      'exact_listing_applied':evidence.get('listing_after',{}).get('applied'),
                      'exact_history_id_matches':evidence.get('history_after',{}).get('exact_id_matches'),
                      'generic_restored_verified':evidence.get('generic_restored_verified'),
                      'error_type':evidence.get('error_type'),
                      'outcome_path':str(OUTCOME.relative_to(ROOT)),
                      'confirmation_path':evidence.get('confirmation_path')},ensure_ascii=False))
    if evidence.get('error_type') or not evidence.get('generic_restored_verified'):
        raise SystemExit(1)


if __name__=='__main__':main()
