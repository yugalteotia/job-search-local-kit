"""One exact, claimed Naukri native-Apply Save for Infosys 190826038395.

The four daily runs remain read-only. This script requires the exact previous
full-history audit and SQLite NeedsReview lock, then performs one user-authorized
provider-specific application attempt in the isolated Chrome profile.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from urllib.parse import urlsplit

from infosys_full_history_audit import snapshot
from infosys_resume_flow import (AUDIT, GENERIC, JOB_DIR, JOB_ID, REQ, ROLE,
                                 check_authorization, current_filename,
                                 inspect_chat, now, sha, upload_profile)
from naukri_direct_playwright import PROFILE_URL, ROOT, _chrome, _status
from job_db import begin_attempt, confirm_attempt, connect, record_evidence, transition
from job_views import regenerate
from naukri_phase_guard import NaukriPhaseGuard, APPLY_PATH, CHAT_PATH

PRESUBMIT=JOB_DIR/'naukri-native-presubmit.json'
OUTCOME=JOB_DIR/'naukri-native-attempt-outcome.json'
CONFIRMATION=JOB_DIR/'naukri-native-confirmation.json'
FORM_URL='https://www.naukri.com/job-listings-java-backend-developer-spring-boot-microservices-infosys-pune-chennai-bengaluru-2-to-5-years-190826038395'
QUESTION='Are you available for a interview?(Virtual)'


def redacted_chat(check):
    """Store UI state only; never persist selected answer or form text."""
    return {key:check.get(key) for key in
            ('drawerVisible','yesOptionVisible','visibleUploadInputs','visibleResumeNames',
             'thankYouVisible','appliedStatusVisible','applyButtonVisible')}


def listing_state(page,url):
    page.goto(url,wait_until='domcontentloaded',timeout=30000)
    page.wait_for_timeout(1600)
    return page.evaluate("""req => {
      const buttons=[...document.querySelectorAll('#apply-button')].filter(e=>e.getClientRects().length);
      const text=document.body.innerText;
      return {path:location.pathname,title:document.title,exactIdInPath:location.pathname.endsWith(req),
        applyVisible:buttons.some(e=>e.innerText.trim()==='Apply'),
        appliedButtonVisible:buttons.some(e=>/Applied/i.test(e.innerText)),
        appliedStatusVisible:/(^|\\n)Applied(\\n|$)/.test(text),
        pendingStatusVisible:/Continue application|Pending application/i.test(text)};
    }""",REQ)


def applied_history_state(page):
    page.goto('https://www.naukri.com/myapply/historypage',wait_until='domcontentloaded',timeout=30000)
    page.wait_for_timeout(1600)
    source=page.locator('.apply-filters li').filter(has_text='Applies on Naukri')
    if source.count()!=1:raise RuntimeError('Applied Jobs source filter unavailable')
    source.click()
    page.wait_for_timeout(1200)
    sweeps=[]
    for _ in range(12):
        item=snapshot(page)
        item['checked_at']=now()
        sweeps.append(item)
        if item['exactIdMatches'] or item['companyRoleMatches']:
            break
        labels=' '.join(item['sourceFilterLabels'])
        import re
        match=re.search(r'Applies on Naukri\s*\((\d+)\)',labels)
        target=int(match.group(1)) if match else None
        if target is not None and item['loadedRows']>=target:
            break
        if not item['loadMorePresent'] or not item['loadMoreVisible']:
            break
        more=page.locator('#loadMoreLnk')
        more.scroll_into_view_if_needed(timeout=5000)
        more.click(timeout=5000,force=True)
        page.wait_for_timeout(1200)
    final=sweeps[-1]
    import re
    count=re.search(r'Applies on Naukri\s*\((\d+)\)',' '.join(final['sourceFilterLabels']))
    return {'url':page.url,'sweeps':[x['loadedRows'] for x in sweeps],
            'final':final,'full_history_loaded':bool(count and final['loadedRows']>=int(count.group(1))),'checked_at':now()}


def write_outcome(data):
    OUTCOME.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')


def main():
    url=check_authorization()
    if url!=FORM_URL:raise RuntimeError('Exact Infosys form URL mismatch')
    prior=json.loads(AUDIT.read_text())
    if prior['final']['loadedRows']!=74:
        raise RuntimeError('Full 74-row prior-history check missing')
    review=json.loads((JOB_DIR/'resume_visual_review.json').read_text())
    if review.get('status')!='visually_clean' or review.get('pdf_sha256')!=sha(ROLE):
        raise RuntimeError('Exact role PDF visual review or SHA mismatch')
    evidence={'job_id':JOB_ID,'requisition_id':REQ,'url':url,'started_at':now(),
              'prior_full_history':str(AUDIT.relative_to(ROOT)),
              'resume_sha256':sha(ROLE),'attempt_id':None,'save_click_count':0,
              'chat_answer_requests_allowed':0,'outcome':'Unstarted'}
    restore_required=False
    route_handler=None
    with _chrome(headless=False) as context:
        page=context.pages[0] if context.pages else context.new_page()
        try:
            if not _status(page)['signed_in']:
                raise RuntimeError('Isolated Naukri session not signed in')
            account_match=page.evaluate("() => /Yugal\\s+Teotia/i.test(document.body.innerText)")
            if not account_match:
                raise RuntimeError('Signed-in profile identity mismatch')
            if current_filename(page)!=GENERIC.name:
                raise RuntimeError('Generic resume was not the initial profile default')
            evidence['signed_in']=True
            evidence['account_name_matches']=True
            restore_required=True
            evidence['role_upload']=upload_profile(page,ROLE)
            if evidence['role_upload']['after']!=ROLE.name:
                raise RuntimeError('Role PDF not displayed in Naukri profile')
            evidence['profile_verified_at']=evidence['role_upload']['at']
            evidence['preflow_listing']=listing_state(page,url)
            evidence['listing_checked_at']=now()
            state=evidence['preflow_listing']
            if (not state['exactIdInPath'] or not state['applyVisible'] or
                state['appliedButtonVisible'] or state['appliedStatusVisible'] or
                state['pendingStatusVisible']):
                raise RuntimeError('Exact listing is no longer live and unapplied')

            phase_guard=NaukriPhaseGuard(REQ)
            show_response=[]
            answer_response=[]
            attempt_id=None
            def on_response(response):
                path=urlsplit(response.url).path
                if path not in (APPLY_PATH,CHAT_PATH):
                    return
                pending=next((event for event in phase_guard.events
                              if event['path']==path and event['allowed'] and event['response_status'] is None),None)
                item={'status':response.status,'path':path}
                try:
                    packet=response.json()
                    node=packet.get('chatbotResponse',{}) if isinstance(packet,dict) else {}
                    item.update({'isApply':node.get('isApply'),'isLeafNode':node.get('isLeafNode'),
                                 'dataCommitted':node.get('dataCommitted'),
                                 'currentNodeName':node.get('currentNodeName'),
                                 'jobIdMentioned':REQ in json.dumps(packet,ensure_ascii=False),
                                 'roleFilenameMentioned':ROLE.name in json.dumps(packet,ensure_ascii=False)})
                except Exception:item['jsonParsed']=False
                phase_guard.response(pending,response.status,{
                    'isApply':item.get('isApply') if isinstance(item.get('isApply'),bool) else None,
                    'isLeafNode':item.get('isLeafNode') if isinstance(item.get('isLeafNode'),bool) else None,
                    'dataCommitted':item.get('dataCommitted') if isinstance(item.get('dataCommitted'),bool) else None,
                    'job_id_match':item.get('jobIdMentioned') if isinstance(item.get('jobIdMentioned'),bool) else None,
                    'currentNodeName':item.get('currentNodeName')})
                (show_response if path.endswith('/apply') else answer_response).append(item)
            page.on('response',on_response)

            def guard(route):
                req=route.request
                path=urlsplit(req.url).path
                if req.method in ('GET','HEAD','OPTIONS'):
                    route.continue_();return
                try:packet=req.post_data_json if path in (APPLY_PATH,CHAT_PATH) else None
                except Exception:packet=None
                if path==CHAT_PATH and isinstance(packet,dict):
                    answer=packet.get('input',{}).get('text') if isinstance(packet.get('input'),dict) else None
                    exact_yes=(answer==['Yes']) or (isinstance(answer,str) and answer.strip()=='Yes')
                    if not exact_yes:
                        phase_guard._event(path,req.method,None,False,'unconfirmed_chat_answer')
                        route.abort();return
                allowed,_event=phase_guard.decide(req.method,req.url,packet)
                if allowed:route.continue_()
                else:route.abort()
            route_handler=guard
            context.route('**/*',guard)
            page.locator('#apply-button').first.click(timeout=10000)
            page.wait_for_timeout(1800)
            evidence['apply_show_count']=phase_guard.show_count
            evidence['show_response']=show_response
            initial=inspect_chat(page)
            evidence['chat_before_answer']=redacted_chat(initial)
            if phase_guard.show_count!=1 or phase_guard.phase!='chat_ready' or not initial['drawerVisible']:
                raise RuntimeError('One exact Apply show did not open the chat')
            if initial['thankYouVisible'] or initial['yesChecked']:
                evidence['outcome']='Saved answer or completion already visible; no second answer sent'
                return
            if initial['prompts']!=[QUESTION] or not initial['yesOptionVisible']:
                evidence['outcome']='Unknown screening question; no answer sent'
                return

            yes_label=page.locator('.chatbot_Drawer label[for="Yes"]')
            if yes_label.count()!=1:raise RuntimeError('Exact Yes label unavailable')
            yes_label.click(timeout=10000)
            if not page.locator('.chatbot_Drawer #Yes').is_checked():
                raise RuntimeError('Confirmed Yes answer was not selected')
            save=page.locator('.chatbot_Drawer .sendMsgbtn_container')
            if save.count()!=1 or save.inner_text().strip()!='Save':
                raise RuntimeError('Potentially submitting chat Save was not identified exactly')
            # Provider-specific meaning: Naukri attaches the profile resume;
            # the freshly reloaded profile displayed the exact role PDF.
            packet={'job_id':JOB_ID,'portal_job_id':REQ,'form_url':url,
                    'provider':'naukri_native_chat','signed_in':True,'account_name_matches':True,
                    'exact_listing_id':REQ,'profile_resume_filename':ROLE.name,
                    'profile_resume_sha256':sha(ROLE),
                    'profile_verified_at':evidence['profile_verified_at'],
                    'listing_checked_at':evidence['listing_checked_at'],
                    'screening_question':QUESTION,'screening_answer_confirmed':True,
                    'potential_submit_action':'screening_chat_save',
                    'profile_resume_binding_method':'current displayed Naukri profile advanced resume',
                    'per_application_picker_visible':False,
                    'prior_full_history_path':str(AUDIT.relative_to(ROOT)),
                    'checks':{'listing_live':True,'not_previously_applied':True,
                              'jd_reviewed':True,'resume_visually_reviewed':True,
                              'exact_resume_selected':True,'answers_confirmed':True,
                              'final_submit_identified':True},
                    'recorded_at':now()}
            PRESUBMIT.write_text(json.dumps(packet,ensure_ascii=False,indent=2)+'\n')
            db=connect()
            try:
                resume_path=str(ROLE.relative_to(ROOT))
                form_path=str(PRESUBMIT.relative_to(ROOT))
                resume_sha=record_evidence(db,JOB_ID,'resume_review',resume_path)
                form_sha=record_evidence(db,JOB_ID,'form_review',form_path)
                transition(db,JOB_ID,'Preparing','infosys_native_pilot','Full history absent; exact role PDF displayed on isolated profile',form_path,expected_state='NeedsReview')
                transition(db,JOB_ID,'Prepared','infosys_native_pilot','Exact screening Yes selected; Save identified as potentially submitting',form_path,expected_state='Preparing')
                attempt_id=begin_attempt(db,JOB_ID,resume_path,resume_sha,url,form_path,form_sha,pilot=True)
                phase_guard.claim(attempt_id)
                evidence['attempt_id']=attempt_id
                evidence['presubmit_path']=form_path
                evidence['claimed_at']=now()
            finally:db.close()
            write_outcome(evidence)

            # The atomic claim exists before this one potentially submitting click.
            phase_guard.arm_save(action_label=save.inner_text().strip(),exact_chat=initial['drawerVisible'],
                                 confirmed_answer=page.locator('.chatbot_Drawer #Yes').is_checked())
            evidence['save_click_count']=1
            save.click(timeout=10000)
            page.wait_for_timeout(2500)
            evidence['chat_answer_requests_allowed']=phase_guard.answer_count
            evidence['chat_after_save']=redacted_chat(inspect_chat(page))
            evidence['answer_response']=answer_response
            evidence['network_guard']=phase_guard.summary()
            evidence['save_completed_at']=now()
            write_outcome(evidence)

            # Positive proof must be bound to the exact requisition, not merely
            # to the chatbot's generic Thank-you sentence.
            listing_after=listing_state(page,url)
            evidence['listing_after_save']=listing_after
            history_after=applied_history_state(page)
            evidence['history_after_save']=history_after
            positive_listing=(listing_after['exactIdInPath'] and
                              (listing_after['appliedButtonVisible'] or listing_after['appliedStatusVisible']))
            positive_history=history_after.get('full_history_loaded') is True and history_after['final']['exactIdMatches']>0
            if positive_listing or positive_history:
                captured=now()
                proof={'attempt_id':attempt_id,'requisition_id':REQ,'captured_at':captured,
                       'url':url if positive_listing else history_after['url'],
                       'confirmation_text':'Applied' if positive_listing else 'Exact requisition present in Naukri Applied Jobs history',
                       'exact_listing_applied':positive_listing,'exact_history_id_match':positive_history,'history_full':history_after.get('full_history_loaded') is True,
                       'history_matching_rows':history_after['final']['matchingRows']}
                CONFIRMATION.write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n')
                db=connect()
                try:
                    confirm_attempt(db,attempt_id,JOB_ID,str(CONFIRMATION.relative_to(ROOT)),captured,pilot=True)
                finally:db.close()
                evidence['outcome']='Applied with exact job-bound proof'
                evidence['confirmation_path']=str(CONFIRMATION.relative_to(ROOT))
            else:
                evidence['outcome']='Uncertain: chat Save sent once, no exact listing or history proof; locked against retry'
            evidence['outcome_checked_at']=now()
            write_outcome(evidence)
        except Exception as exc:
            evidence['error_type']=type(exc).__name__
            evidence['outcome']=evidence.get('outcome') if evidence.get('outcome') not in ('Unstarted',None) else 'Stopped before or during one authorized attempt'
            evidence['error_at']=now()
            write_outcome(evidence)
            raise
        finally:
            if restore_required:
                try:
                    if route_handler is not None:
                        context.unroute('**/*',route_handler)
                    evidence['generic_restore']=upload_profile(page,GENERIC)
                    evidence['generic_restored_verified']=current_filename(page)==GENERIC.name
                except Exception as exc:
                    evidence['generic_restored_verified']=False
                    evidence['restore_error_type']=type(exc).__name__
                evidence['finished_at']=now()
                write_outcome(evidence)
                print(json.dumps({'job_id':JOB_ID,'attempt_id':evidence.get('attempt_id'),
                                  'outcome':evidence.get('outcome'),
                                  'save_click_count':evidence.get('save_click_count'),
                                  'chat_answer_requests_allowed':evidence.get('chat_answer_requests_allowed'),
                                  'generic_restored_verified':evidence.get('generic_restored_verified'),
                                  'evidence_path':str(OUTCOME.relative_to(ROOT)),
                                  'confirmation_path':evidence.get('confirmation_path'),
                                  'error_type':evidence.get('error_type')},ensure_ascii=False),flush=True)
    regenerate()


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'error_type':type(exc).__name__,
                          'error':str(exc) if isinstance(exc,RuntimeError) else 'Pilot operation failed'}))
        raise SystemExit(1)
