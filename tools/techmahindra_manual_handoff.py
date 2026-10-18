"""One manual Apply handoff, then continue the same Tech Mahindra attempt."""
import datetime as dt
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, 'tools')
from naukri_direct_playwright import ROOT, _chrome, _status
from infosys_resume_flow import current_filename, upload_profile
from naukri_phase_guard import NaukriPhaseGuard, APPLY_PATH, CHAT_PATH
from techmahindra_native_apply_once import (JOB_ID, REQ, FOLDER, ROLE, GENERIC, URL,
    exact_listing, chat_snapshot, history_snapshot)
from techmahindra_resume_existing_attempt import saved_gate, NOTICE_PROMPT, NOTICE_ANSWER
from job_db import connect, confirm_attempt
from job_views import regenerate

RESULT = FOLDER / 'naukri-native-manual-handoff.json'
CONFIRM = FOLDER / 'naukri-native-confirmation.json'


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def main():
    prior, job, attempt, checks = saved_gate()
    result = {'job_id': JOB_ID, 'requisition_id': REQ, 'attempt_id': attempt['attempt_id'],
        'started_at': now(), 'precondition_checks': checks, 'new_attempt_created': False,
        'previous_save_replayed': False, 'notice_answer_save_count': 0, 'apply_clicked_by_agent': False,
        'save_clicked_by_agent': False, 'outcome': 'Starting visible manual handoff', 'network_responses': []}
    guard = None
    route_guard = None
    context = None
    page = None
    try:
        with _chrome(headless=False) as context:
            page = context.pages[0] if context.pages else context.new_page()
            if not _status(page)['signed_in'] or not page.evaluate("()=>/Yugal\\s+Teotia/i.test(document.body.innerText)"):
                raise RuntimeError('Isolated signed-in Yugal account not verified')
            if current_filename(page) != GENERIC.name:
                raise RuntimeError('Generic profile PDF is not the current starting resume')
            result['signed_in_account_verified'] = True
            result['generic_resume_before'] = GENERIC.name
            upload = upload_profile(page, ROLE)
            if upload['after'] != ROLE.name:
                raise RuntimeError('Role-specific PDF did not become the active profile resume')
            result['role_resume_verified'] = True
            result['profile_upload_statuses'] = upload['response_statuses']

            listing = exact_listing(page)
            if (not listing['exact_path'] or not listing['apply_visible'] or listing['applied_button']
                    or listing['applied_status'] or listing['pending']):
                raise RuntimeError('Fresh exact listing check failed before manual handoff')
            result['listing_before_handoff'] = listing
            result['listing_checked_at'] = now()

            guard = NaukriPhaseGuard(REQ)
            pending = []

            def on_response(response):
                path = urlsplit(response.url).path
                if path not in (APPLY_PATH, CHAT_PATH):
                    return
                event = next((item for item in pending if item['path'] == path and item['response_status'] is None), None)
                metadata = {}
                try:
                    body = response.json()
                    node = body.get('chatbotResponse', {}) if isinstance(body, dict) else {}
                    metadata = {key: node.get(key) for key in ('isApply', 'isLeafNode', 'dataCommitted')
                        if isinstance(node.get(key), bool)}
                    metadata['job_id_match'] = REQ in json.dumps(body, ensure_ascii=False)
                    if isinstance(node.get('currentNodeName'), str):
                        metadata['currentNodeName'] = node['currentNodeName']
                except Exception:
                    pass
                guard.response(event, response.status, metadata)
                result['network_responses'].append({'path': path,
                    'flowtype': event['flowtype'] if event else 'unknown',
                    'reason': event['reason'] if event else 'unmatched_response',
                    'status': response.status,
                    'response_metadata': {k: v for k, v in metadata.items() if v is not None}})

            page.on('response', on_response)

            def route_guard(route):
                request = route.request
                path = urlsplit(request.url).path
                if request.method in ('GET', 'HEAD', 'OPTIONS'):
                    route.continue_()
                    return
                try:
                    payload = request.post_data_json if path in (APPLY_PATH, CHAT_PATH) else None
                except Exception:
                    payload = None
                allowed, event = guard.decide(request.method, request.url, payload)
                if event:
                    pending.append(event)
                if path == CHAT_PATH:
                    ready = any(item.get('answer_ready') is True for item in result.get('answer_gate', []))
                    if not ready:
                        route.abort()
                        return
                    result['answer_gate'][-1]['answer_ready'] = False
                if allowed:
                    route.continue_()
                else:
                    route.abort()

            route_guard = route_guard
            page.route('**/*', route_guard)
            page.bring_to_front()
            result['manual_handoff_ready_at'] = now()
            result['visible_apply_control'] = True
            print(json.dumps({'manual_handoff_ready': True, 'url': URL,
                'title': listing.get('title'), 'apply_visible': True,
                'already_applied': False, 'active_resume': ROLE.name,
                'save_or_submit_clicked_by_agent': False}, ensure_ascii=False), flush=True)
            input('Click the visible Apply button once in this Chrome window. Leave Save untouched, then tell Codex you are done.\n')
            page.wait_for_timeout(1800)
            ui = chat_snapshot(page)
            latest = next((prompt for prompt in reversed(ui['prompts'])
                if not prompt.lower().startswith('hi yugal')), '')
            result['questionnaire_after_handoff'] = {'drawer': ui['drawer'], 'prompts': ui['prompts'],
                'editable_count': ui['editable_count'], 'editable_filled': ui['editable_filled'],
                'save_controls': ui['save_controls'], 'thank_you': ui['thank_you']}
            result['guard_after_handoff'] = guard.summary()
            if (guard.phase != 'chat_ready' or guard.show_count != 1 or not ui['drawer']
                    or latest.strip() != NOTICE_PROMPT or ui['editable_count'] != 1
                    or ui['editable_filled'] != [False] or ui['thank_you']):
                result['outcome'] = 'Stopped: current UI was not the unanswered notice-period question'
                return

            # Claim the existing attempt ID only; never create a second attempt.
            guard.claim(attempt['attempt_id'])
            editor = page.locator('.chatbot_Drawer [contenteditable=true]')
            editor.fill(NOTICE_ANSWER, timeout=5000)
            if editor.inner_text().strip() != NOTICE_ANSWER:
                raise RuntimeError('Confirmed date answer did not remain in the visible editor')
            save = page.locator('.chatbot_Drawer .sendMsgbtn_container')
            if (save.count() != 1 or save.inner_text().strip() != 'Save'
                    or 'disabled' in (save.get_attribute('class') or '')):
                raise RuntimeError('Questionnaire Save control is not uniquely enabled')
            result['answer_gate'] = [{'question_index': 2, 'answer_ready': True}]
            guard.arm_save(action_label='Save', exact_chat=True,
                confirmed_answer=editor.inner_text().strip() == NOTICE_ANSWER)
            result['notice_answer_save_count'] = 1
            result['save_clicked_by_agent'] = True
            save.click(timeout=10000)
            page.wait_for_timeout(3500)
            after = chat_snapshot(page)
            result['questionnaire_after_notice_save'] = {'drawer': after['drawer'], 'prompts': after['prompts'],
                'editable_count': after['editable_count'], 'editable_filled': after['editable_filled'],
                'save_controls': after['save_controls'], 'thank_you': after['thank_you'],
                'applied_visible': after['applied_visible']}
            result['network_guard'] = guard.summary()
            listing_after = exact_listing(page)
            history_after = history_snapshot(page)
            result['listing_after'] = listing_after
            result['history_after'] = history_after
            completion = [event for event in guard.events if event['reason'] == 'claimed_save_completion']
            network_ok = bool(completion and isinstance(completion[-1]['response_status'], int)
                and 200 <= completion[-1]['response_status'] < 300)
            positive_listing = listing_after['exact_path'] and (listing_after['applied_button'] or listing_after['applied_status'])
            positive_history = history_after.get('full_history_loaded') is True and history_after['exact_id_matches'] > 0
            if network_ok and (positive_listing or positive_history):
                captured = now()
                proof = {'attempt_id': attempt['attempt_id'], 'requisition_id': REQ,
                    'captured_at': captured, 'url': URL if positive_listing else 'https://www.naukri.com/myapply/historypage',
                    'confirmation_text': 'Exact Tech Mahindra requisition is shown as applied in Naukri',
                    'exact_listing_applied': bool(positive_listing), 'exact_history_id_match': bool(positive_history), 'history_full': history_after.get('full_history_loaded') is True,
                    'completion_flowtype': completion[-1]['flowtype'],
                    'completion_http_status': completion[-1]['response_status']}
                CONFIRM.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n')
                db = connect()
                try:
                    confirm_attempt(db, attempt['attempt_id'], JOB_ID,
                        str(CONFIRM.relative_to(ROOT)), captured, pilot=True)
                finally:
                    db.close()
                result['outcome'] = 'Applied with positive job-bound confirmation'
                result['confirmation_path'] = str(CONFIRM.relative_to(ROOT))
            elif after['thank_you'] or guard.completion_count:
                result['outcome'] = 'Uncertain: same attempt locked; no retry without positive job-bound proof'
            else:
                result['outcome'] = 'Questionnaire remains incomplete; same attempt locked; no repeat Save'
    except Exception as exc:
        result['error_type'] = type(exc).__name__
        result['error_message'] = str(exc)
        result['outcome'] = 'Stopped with same attempt ID; no new attempt created'
    finally:
        if page is not None and context is not None:
            try:
                page.unroute('**/*', route_guard)
            except Exception:
                pass
            try:
                restored = upload_profile(page, GENERIC)
                result['generic_restored_verified'] = restored['after'] == GENERIC.name and current_filename(page) == GENERIC.name
                result['generic_restore_statuses'] = restored['response_statuses']
            except Exception as exc:
                result['generic_restored_verified'] = False
                result['generic_restore_error_type'] = type(exc).__name__
        result['finished_at'] = now()
        RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    regenerate()
    print(json.dumps({'job_id': JOB_ID, 'requisition_id': REQ, 'attempt_id': attempt['attempt_id'],
        'outcome': result.get('outcome'), 'notice_answer_save_count': result.get('notice_answer_save_count'),
        'completion_response_2xx': any(e.get('reason') == 'claimed_save_completion'
            and isinstance(e.get('response_status'), int) and 200 <= e['response_status'] < 300
            for e in (result.get('network_guard') or {}).get('events', [])),
        'generic_restored_verified': result.get('generic_restored_verified'),
        'result_path': str(RESULT.relative_to(ROOT)), 'confirmation_path': result.get('confirmation_path')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
