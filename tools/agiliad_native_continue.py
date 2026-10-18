"""Continue the explicitly approved, existing Agiliad Naukri chat attempt once.

This never creates a second attempt or repeats the backend-experience answer.
"""
from __future__ import annotations

import json
import traceback
from urllib.parse import urlsplit

from agiliad_native_apply_once import (CONFIRMATION, FOLDER, GENERIC, JOB_ID,
    OUTCOME, REQ, ROLE, URL, history, listing, now, sha)
from infosys_resume_flow import current_filename, upload_profile
from job_db import connect, confirm_attempt
from job_views import regenerate
from naukri_direct_playwright import ROOT, _chrome, _status
from naukri_phase_guard import APPLY_PATH, CHAT_PATH, NaukriPhaseGuard

QUESTION = 'Are you currently residing in Pune, Maharashtra or willing to relocate to Pune, Maharashtra?'
AUDIT = FOLDER/'naukri-native-continuation.json'


def locked_claim():
    if AUDIT.exists():
        prior=json.loads(AUDIT.read_text())
        if prior.get('second_save_click_count',0)>0:
            raise RuntimeError('The continuation Save was already clicked; no retry permitted')
    db=connect()
    try:
        row=db.execute('SELECT * FROM jobs WHERE id=?',(JOB_ID,)).fetchone()
        attempts=db.execute('SELECT * FROM submission_attempts WHERE job_id=?',(JOB_ID,)).fetchall()
        if (not row or row['requisition_id']!=REQ or row['canonical_url']!=URL
                or row['state']!='SubmissionUnconfirmed' or len(attempts)!=1
                or attempts[0]['state']!='uncertain' or attempts[0]['proof_path']):
            raise RuntimeError('Existing exact-job uncertain claim is unavailable')
        outcome=json.loads(OUTCOME.read_text())
        if (outcome.get('attempt_id')!=attempts[0]['attempt_id']
                or outcome.get('save_click_count')!=1
                or outcome.get('network_guard',{}).get('answer_count')!=1
                or outcome.get('network_guard',{}).get('completion_count')!=0):
            raise RuntimeError('Prior one-answer evidence does not match claim')
        review=json.loads((FOLDER/'resume_visual_review.json').read_text())
        if review.get('pdf_sha256')!=sha(ROLE):
            raise RuntimeError('Role PDF changed since visual review')
        return attempts[0]['attempt_id']
    finally:db.close()


def main():
    attempt_id=locked_claim()
    result={'job_id':JOB_ID,'requisition_id':REQ,'url':URL,'attempt_id':attempt_id,
            'started_at':now(),'second_save_click_count':0,'outcome':'Stopped before next Save',
            'scheduled_submissions_enabled':False}
    restore=False;handler=None
    with _chrome(headless=False) as context:
        page=context.pages[0] if context.pages else context.new_page()
        try:
            if not _status(page)['signed_in'] or not page.evaluate("() => /Yugal\\s+Teotia/i.test(document.body.innerText)"):
                raise RuntimeError('Isolated Naukri account identity mismatch')
            if current_filename(page)!=GENERIC.name:
                raise RuntimeError('Generic resume not current before continuation')
            restore=True
            before=history(page);result['history_before']=before
            if not before['full_source_loaded'] or before['exact_id_matches']:
                raise RuntimeError('Exact job appeared in Applied Jobs before continuation')
            result['role_upload']=upload_profile(page,ROLE)
            if result['role_upload']['after']!=ROLE.name:
                raise RuntimeError('Role resume not displayed before application flow')
            state=listing(page);result['listing_before']=state
            if not state['exact'] or not state['apply'] or state['applied'] or state['pending']:
                raise RuntimeError('Exact Naukri listing is no longer live and unapplied')
            guard=NaukriPhaseGuard(REQ)
            responses=[]
            def on_response(response):
                path=urlsplit(response.url).path
                if path not in (APPLY_PATH,CHAT_PATH):return
                event=next((e for e in guard.events if e['path']==path and e['allowed'] and e['response_status'] is None),None)
                item={'path':path,'status':response.status,'job_id_match':False,
                      'is_apply':None,'data_committed':None}
                try:
                    body=response.json(); node=body.get('chatbotResponse',{}) if isinstance(body,dict) else {}
                    item['job_id_match']=REQ in json.dumps(body,ensure_ascii=False)
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
                try:packet=request.post_data_json if path in (APPLY_PATH,CHAT_PATH) else None
                except Exception:packet=None
                if path==CHAT_PATH:
                    value=packet.get('input',{}).get('text') if isinstance(packet,dict) and isinstance(packet.get('input'),dict) else None
                    if value not in (['Yes'],'Yes'):
                        guard._event(path,request.method,None,False,'answer_not_confirmed')
                        route.abort();return
                allowed,_=guard.decide(request.method,request.url,packet)
                if allowed:route.continue_()
                else:route.abort()
            handler=intercept;context.route('**/*',intercept)
            page.locator('#apply-button').first.click(timeout=10000)
            page.wait_for_timeout(1600)
            chat=page.evaluate('''() => {const d=[...document.querySelectorAll('.chatbot_Drawer')].find(x=>x.getClientRects().length);
              return {drawer:!!d,prompts:[...(d?.querySelectorAll('.botMsg')||[])].filter(x=>x.getClientRects().length).map(x=>x.innerText.trim()),
                yes:!!d?.querySelector('input[type=radio]#Yes'),no:!!d?.querySelector('input[type=radio]#No'),
                yes_checked:!!d?.querySelector('input[type=radio]#Yes:checked'),
                thank_you:/(Thank you for your responses)/i.test(d?.innerText||'')};}''')
            result['chat_before']={'drawer':chat['drawer'],'prompt_count':len(chat['prompts']),
                'last_prompt_matches':bool(chat['prompts']) and chat['prompts'][-1]==QUESTION,
                'yes_visible':chat['yes'],'no_visible':chat['no'],'thank_you':chat['thank_you']}
            if (guard.phase!='chat_ready' or not chat['drawer'] or not chat['prompts']
                    or chat['prompts'][-1]!=QUESTION or not chat['yes'] or not chat['no']
                    or chat['thank_you']):
                raise RuntimeError('Saved Pune relocation question is not the active exact-job question')
            yes=page.locator('.chatbot_Drawer input[type=radio]#Yes')
            label=page.locator('.chatbot_Drawer label[for="Yes"]')
            if label.count()!=1:
                raise RuntimeError('Visible Yes label for custom radio is unavailable')
            label.click(timeout=5000)
            if not yes.is_checked():raise RuntimeError('Confirmed Pune Yes answer did not remain selected')
            save=page.locator('.chatbot_Drawer .sendMsgbtn_container')
            if save.count()!=1 or save.inner_text().strip()!='Save':
                raise RuntimeError('Expected one potentially submitting Save action')
            guard.claim(attempt_id)
            guard.arm_save(action_label='Save',exact_chat=True,confirmed_answer=yes.is_checked())
            result['question_confirmed']=True;result['role_resume_sha256']=sha(ROLE)
            result['second_save_click_count']=1;AUDIT.write_text(json.dumps(result,indent=2)+'\n')
            save.click(timeout=10000)
            page.wait_for_timeout(2500)
            result['network_guard']=guard.summary();result['network_responses']=responses
            result['chat_after_save']=page.evaluate('''() => {const d=document.querySelector('.chatbot_Drawer');
                return {thank_you:/(Thank you for your responses)/i.test(d?.innerText||''),
                  question_count:[...(d?.querySelectorAll('.botMsg')||[])].filter(x=>x.getClientRects().length).length};}''')
            result['listing_after']=listing(page);result['history_after']=history(page)
            completion=[e for e in guard.events if e['reason']=='claimed_save_completion']
            completed=(len(completion)==1 and completion[0]['response_status'] is not None
                and 200<=completion[0]['response_status']<300)
            positive=(result['listing_after']['exact'] and result['listing_after']['applied']) or (result['history_after'].get('full_source_loaded') is True and result['history_after']['exact_id_matches']>0)
            if completed and positive:
                captured=now()
                proof={'attempt_id':attempt_id,'requisition_id':REQ,'captured_at':captured,'url':URL,
                       'confirmation_text':'Applied on exact listing' if result['listing_after']['applied'] else 'Exact job ID in Naukri Applied Jobs',
                       'completion_flowtype':completion[0]['flowtype'],'completion_http_status':completion[0]['response_status'],
                       'exact_listing_applied':result['listing_after']['applied'],
                       'exact_history_id_match':result['history_after'].get('full_source_loaded') is True and result['history_after']['exact_id_matches']>0,
                       'history_full':result['history_after'].get('full_source_loaded') is True}
                CONFIRMATION.write_text(json.dumps(proof,indent=2)+'\n')
                db=connect()
                try:confirm_attempt(db,attempt_id,JOB_ID,str(CONFIRMATION.relative_to(ROOT)),captured,pilot=True)
                finally:db.close()
                result['outcome']='Applied with exact job-bound proof'
                result['confirmation_path']=str(CONFIRMATION.relative_to(ROOT))
            else:
                result['outcome']='Uncertain; second Save clicked, no retry permitted'
            result['completion_network_2xx']=completed;result['job_bound_positive']=positive
        except Exception as exc:
            result['error_type']=type(exc).__name__
            result['error_detail']=str(exc).split('\n')[0][:180]
            frame=traceback.extract_tb(exc.__traceback__)[-1]
            result['error_at_code']={'function':frame.name,'line':frame.lineno}
            result['outcome']='Uncertain; no retry' if result['second_save_click_count'] else 'Stopped before next Save'
        finally:
            if handler is not None:context.unroute('**/*',handler)
            if restore:
                try:
                    result['generic_restore']=upload_profile(page,GENERIC)
                    result['generic_restored_verified']=current_filename(page)==GENERIC.name
                except Exception as exc:
                    result['generic_restored_verified']=False
                    result['restore_error_type']=type(exc).__name__
            result['finished_at']=now();AUDIT.write_text(json.dumps(result,indent=2)+'\n')
    regenerate()
    print(json.dumps({'job_id':JOB_ID,'attempt_id':attempt_id,'outcome':result['outcome'],
        'second_save_click_count':result['second_save_click_count'],
        'completion_network_2xx':result.get('completion_network_2xx'),
        'job_bound_positive':result.get('job_bound_positive'),
        'generic_restored_verified':result.get('generic_restored_verified'),
        'error_type':result.get('error_type'),'audit_path':str(AUDIT.relative_to(ROOT))},ensure_ascii=False))
    if result.get('error_type') or not result.get('generic_restored_verified'):raise SystemExit(1)


if __name__=='__main__':main()
