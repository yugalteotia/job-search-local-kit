"""One bounded Agiliad native-Apply inspection; no answer or Save is allowed."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from infosys_resume_flow import current_filename, inspect_chat, now, upload_profile
from naukri_direct_playwright import ROOT, _chrome, _status
from naukri_phase_guard import APPLY_PATH, CHAT_PATH, NaukriPhaseGuard

REQ='280926023134'
URL='https://www.naukri.com/job-listings-java-developer-senior-java-developer-agiliad-pune-2-to-6-years-280926023134'
FOLDER=ROOT/'jobs/agiliad-java-backend-developer-280926023134'
ROLE=FOLDER/'Yugal_Teotia_Java_Backend_Developer.pdf'
GENERIC=ROOT/'master-resume/Yugal_Teotia_CV.pdf'
OUT=FOLDER/'native-flow-inspection.json'


def main():
    if not ROLE.is_file() or not GENERIC.is_file():raise RuntimeError('Resume file missing')
    review=json.loads((FOLDER/'resume_visual_review.json').read_text())
    if review.get('status')!='visually_clean' or review.get('pdf')!=ROLE.name:
        raise RuntimeError('Role PDF has not passed visual review')
    result={'requisition_id':REQ,'job_url':URL,'started_at':now(),
            'form_opened':False,'screening_answers_sent':0,'save_clicked':False,
            'submission_attempted':False}
    restore=False;handler=None
    with _chrome(headless=False) as context:
        page=context.pages[0] if context.pages else context.new_page()
        try:
            if not _status(page)['signed_in']:raise RuntimeError('Isolated Naukri session signed out')
            if current_filename(page)!=GENERIC.name:raise RuntimeError('Generic profile resume not current')
            restore=True
            result['role_upload']=upload_profile(page,ROLE)
            page.goto(URL,wait_until='domcontentloaded',timeout=30000)
            page.wait_for_timeout(1000)
            if urlsplit(page.url).path!=urlsplit(URL).path:
                raise RuntimeError('Exact Naukri listing changed')
            button=page.locator('#apply-button').first
            if button.count()!=1 or button.inner_text().strip()!='Apply':
                raise RuntimeError('Listing no longer shows native Apply')
            guard=NaukriPhaseGuard(REQ)
            def on_response(response):
                path=urlsplit(response.url).path
                if path not in (APPLY_PATH,CHAT_PATH):return
                pending=next((event for event in guard.events if event['path']==path
                              and event['allowed'] and event['response_status'] is None),None)
                guard.response(pending,response.status)
            page.on('response',on_response)
            def intercept(route):
                req=route.request
                try:packet=req.post_data_json if urlsplit(req.url).path in (APPLY_PATH,CHAT_PATH) else None
                except Exception:packet=None
                allowed,_=guard.decide(req.method,req.url,packet)
                if allowed:route.continue_()
                else:route.abort()
            handler=intercept
            context.route('**/*',intercept)
            button.click(timeout=10000)
            page.wait_for_timeout(1800)
            chat=inspect_chat(page)
            result['form_opened']=chat['drawerVisible']
            result['question_texts']=chat['prompts']
            result['yes_option_visible']=chat['yesOptionVisible']
            result['visible_upload_inputs']=chat['visibleUploadInputs']
            result['visible_resume_names']=chat['visibleResumeNames']
            result['save_control_count']=page.locator('.chatbot_Drawer .sendMsgbtn_container').count()
            result['network_guard']=guard.summary()
            result['inspected_at']=now()
        except Exception as exc:
            result['error_type']=type(exc).__name__
            result['error_at']=now()
        finally:
            if handler is not None:context.unroute('**/*',handler)
            if restore:
                try:
                    result['generic_restore']=upload_profile(page,GENERIC)
                    result['generic_restored_verified']=current_filename(page)==GENERIC.name
                except Exception as exc:
                    result['generic_restored_verified']=False
                    result['restore_error_type']=type(exc).__name__
            result['finished_at']=now()
            OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'requisition_id':REQ,'form_opened':result['form_opened'],
                      'question_texts':result.get('question_texts'),
                      'save_control_count':result.get('save_control_count'),
                      'generic_restored_verified':result.get('generic_restored_verified'),
                      'error_type':result.get('error_type'),
                      'network_guard':result.get('network_guard'),
                      'evidence_path':str(OUT.relative_to(ROOT))},ensure_ascii=False))
    if result.get('error_type') or not result.get('generic_restored_verified'):
        raise SystemExit(1)


if __name__=='__main__':main()
