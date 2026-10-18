"""Resume only the proven-incomplete Tech Mahindra questionnaire attempt."""
import datetime as dt
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, 'tools')
from naukri_direct_playwright import ROOT, _chrome, _status
from infosys_resume_flow import current_filename, upload_profile, sha
from naukri_phase_guard import NaukriPhaseGuard, APPLY_PATH, CHAT_PATH
from techmahindra_native_apply_once import (JOB_ID, REQ, FOLDER, ROLE, GENERIC, URL,
    OUT as PREVIOUS, exact_listing, chat_snapshot, history_snapshot)
from job_db import connect, record_evidence, confirm_attempt
from job_views import regenerate

RESULT = FOLDER / 'naukri-native-recovery-result.json'
CONFIRM = FOLDER / 'naukri-native-confirmation.json'
NOTICE_PROMPT = 'What is your notice period,if serving notice period last working day?'
NOTICE_ANSWER = 'My last working day is 30 September 2026. I am available to join from 1 October 2026.'


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def saved_gate():
    report = json.loads(PREVIOUS.read_text())
    guard = report.get('network_guard') or {}
    ui = report.get('chat_after_save') or {}
    listing = report.get('listing_after') or {}
    history = report.get('history_after') or {}
    db = connect()
    try:
        job = db.execute('select * from jobs where id=?', (JOB_ID,)).fetchone()
        attempts = db.execute('select * from submission_attempts where job_id=?', (JOB_ID,)).fetchall()
    finally:
        db.close()
    checks = {
        'exact_job': bool(job and job['requisition_id'] == REQ and job['canonical_url'] == URL),
        'one_existing_attempt': len(attempts) == 1 and attempts[0]['attempt_id'] == report.get('attempt_id')
            and attempts[0]['state'] == 'uncertain' and attempts[0]['proof_path'] is None,
        'no_completion_request': guard.get('completion_count') == 0 and not any(
            event.get('reason') == 'claimed_save_completion' for event in guard.get('events', [])),
        'no_commit': guard.get('workflow_committed') is False and report.get('completion_response_2xx') is False,
        'no_applied_history_match': history.get('full_history_loaded') is True
            and history.get('exact_id_matches') == 0 and history.get('company_role_matches') == 0,
        'not_applied_and_live': listing.get('exact_path') is True and listing.get('apply_visible') is True
            and not listing.get('applied_button') and not listing.get('applied_status') and not listing.get('pending'),
        'notice_question_waiting': ui.get('drawer') is True and ui.get('thank_you') is False
            and ui.get('editable_count') == 1 and ui.get('editable_filled') == [False]
            and any('notice period' in prompt.lower() for prompt in ui.get('prompts', [])),
        'generic_resume_restored': report.get('generic_restored_verified') is True,
    }
    if not all(checks.values()):
        raise RuntimeError('Saved-evidence recovery gate failed: ' + ', '.join(k for k, v in checks.items() if not v))
    return report, job, attempts[0], checks


def main():
    prior, job, attempt, checks = saved_gate()
    result = {'job_id': JOB_ID, 'requisition_id': REQ, 'attempt_id': attempt['attempt_id'],
        'started_at': now(), 'precondition_checks': checks, 'new_attempt_created': False,
        'previous_save_replayed': False, 'notice_answer_save_count': 0, 'apply_clicked': False,
        'outcome': 'Recovery started',
        'network_responses': []}
    guard = None
    with _chrome(headless=False) as context:
        page = context.pages[0] if context.pages else context.new_page()
        if not _status(page)['signed_in'] or not page.evaluate("()=>/Yugal\\s+Teotia/i.test(document.body.innerText)"):
            raise RuntimeError('Isolated signed-in Yugal account not verified')
        if current_filename(page) != GENERIC.name:
            raise RuntimeError('Generic profile resume is not the current starting resume')
        result['signed_in_account_verified'] = True
        result['generic_resume_before'] = GENERIC.name
        try:
            uploaded = upload_profile(page, ROLE)
            if uploaded['after'] != ROLE.name:
                raise RuntimeError('Role-specific profile resume did not verify')
            result['role_resume_verified'] = True
            result['profile_upload_statuses'] = uploaded['response_statuses']

            listing = exact_listing(page)
            history = history_snapshot(page)
            result['live_listing_before'] = listing
            result['applied_history_before'] = history
            if (not listing['exact_path'] or not listing['apply_visible'] or listing['applied_button']
                    or listing['applied_status'] or listing['pending'] or history['exact_id_matches']
                    or history['company_role_matches']):
                raise RuntimeError('Fresh listing/history check no longer permits continuation')

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

            page.route('**/*', route_guard)
            apply = page.locator('#apply-button:visible').filter(has_text='Apply')
            if apply.count() != 1:
                raise RuntimeError('Exact visible Apply control is unavailable')
            # This opens/retrieves the existing questionnaire only. No answer is sent here.
            apply.click(timeout=10000)
            result['apply_clicked'] = True
            page.wait_for_timeout(4500)
            ui = chat_snapshot(page)
            result['questionnaire_after_reopen'] = {'drawer': ui['drawer'], 'prompts': ui['prompts'],
                'editable_count': ui['editable_count'], 'editable_filled': ui['editable_filled'],
                'save_controls': ui['save_controls'], 'thank_you': ui['thank_you']}
            latest = next((prompt for prompt in reversed(ui['prompts'])
                if not prompt.lower().startswith('hi yugal')), '')
            if (guard.phase != 'chat_ready' or guard.show_count != 1 or not ui['drawer']
                    or latest.strip() != NOTICE_PROMPT or ui['editable_count'] != 1
                    or ui['editable_filled'] != [False] or ui['thank_you']):
                result['outcome'] = 'Stopped before answering: reopened UI was not the unanswered notice-period question'
                result['network_guard'] = guard.summary()
                RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
                return

            guard.claim(attempt['attempt_id'])
            editor = page.locator('.chatbot_Drawer [contenteditable=true]')
            editor.fill(NOTICE_ANSWER, timeout=5000)
            if editor.inner_text().strip() != NOTICE_ANSWER:
                raise RuntimeError('Confirmed notice/availability answer did not remain in the exact visible editor')
            save = page.locator('.chatbot_Drawer .sendMsgbtn_container')
            if (save.count() != 1 or save.inner_text().strip() != 'Save'
                    or 'disabled' in (save.get_attribute('class') or '')):
                raise RuntimeError('Questionnaire Save control is not uniquely enabled')
            result['answer_gate'] = [{'question_index': 2, 'answer_ready': True}]
            guard.arm_save(action_label='Save', exact_chat=True,
                confirmed_answer=editor.inner_text().strip() == NOTICE_ANSWER)
            result['notice_answer_save_count'] = 1
            save.click(timeout=10000)
            page.wait_for_timeout(3500)
            after_ui = chat_snapshot(page)
            result['questionnaire_after_notice_save'] = {'drawer': after_ui['drawer'],
                'prompts': after_ui['prompts'], 'editable_count': after_ui['editable_count'],
                'editable_filled': after_ui['editable_filled'], 'save_controls': after_ui['save_controls'],
                'thank_you': after_ui['thank_you'], 'applied_visible': after_ui['applied_visible']}
            result['network_guard'] = guard.summary()
            result['completion_response_2xx'] = any(e['reason'] == 'claimed_save_completion'
                and isinstance(e['response_status'], int) and 200 <= e['response_status'] < 300
                for e in guard.events)

            listing_after = exact_listing(page)
            history_after = history_snapshot(page)
            result['listing_after'] = listing_after
            result['history_after'] = history_after
            completion = [e for e in guard.events if e['reason'] == 'claimed_save_completion']
            network_ok = bool(completion and isinstance(completion[-1]['response_status'], int)
                and 200 <= completion[-1]['response_status'] < 300)
            positive_listing = listing_after['exact_path'] and (listing_after['applied_button'] or listing_after['applied_status'])
            positive_history = history_after.get('full_history_loaded') is True and history_after['exact_id_matches'] > 0
            if (network_ok and (positive_listing or positive_history)):
                captured = now()
                proof = {'attempt_id': attempt['attempt_id'], 'requisition_id': REQ,
                    'captured_at': captured,
                    'url': URL if positive_listing else 'https://www.naukri.com/myapply/historypage',
                    'confirmation_text': 'Exact Tech Mahindra requisition is shown as applied in Naukri',
                    'exact_listing_applied': bool(positive_listing),
                    'exact_history_id_match': bool(positive_history), 'history_full': history_after.get('full_history_loaded') is True,
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
            elif after_ui['thank_you'] or guard.completion_count:
                result['outcome'] = 'Uncertain: completion may have occurred; same attempt remains locked, no retry'
            else:
                result['outcome'] = 'Questionnaire remains incomplete; same attempt remains locked, no repeat Save'
            result['attempt_id'] = attempt['attempt_id']
            result['finished_at'] = now()
            RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        except Exception as exc:
            result['error_type'] = type(exc).__name__
            result['error_message'] = str(exc)
            result['outcome'] = 'Stopped before notice answer; existing attempt remains locked'
            result['finished_at'] = now()
            RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        finally:
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
        'completion_response_2xx': result.get('completion_response_2xx'),
        'exact_listing_applied': result.get('listing_after', {}).get('applied_button') or result.get('listing_after', {}).get('applied_status'),
        'history_exact_matches': result.get('history_after', {}).get('exact_id_matches'),
        'generic_restored_verified': result.get('generic_restored_verified'),
        'result_path': str(RESULT.relative_to(ROOT)), 'confirmation_path': result.get('confirmation_path')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
