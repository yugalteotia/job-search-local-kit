"""Continue the one existing Tech Mahindra attempt from its blank current-location question."""
import datetime as dt
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, 'tools')
from naukri_direct_playwright import ROOT, _chrome, _status
from infosys_resume_flow import current_filename, upload_profile, sha
from naukri_phase_guard import NaukriPhaseGuard, APPLY_PATH, CHAT_PATH
from techmahindra_native_apply_once import (JOB_ID, REQ, FOLDER, ROLE, GENERIC, URL,
    exact_listing, chat_snapshot, history_snapshot)
from job_db import connect, confirm_attempt
from job_views import regenerate

PRIOR = FOLDER / 'naukri-native-manual-handoff.json'
RESULT = FOLDER / 'naukri-native-continuation.json'
CONFIRM = FOLDER / 'naukri-native-confirmation.json'
CURRENT_LOCATION_QUESTION = 'What is your Current location?'


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def latest_question(ui):
    return next((prompt for prompt in reversed(ui['prompts'])
        if prompt and not prompt.lower().startswith('hi yugal')), '')


def preflight():
    report = json.loads(PRIOR.read_text())
    guard = report.get('network_guard') or {}
    ui = report.get('questionnaire_after_notice_save') or {}
    listing = report.get('listing_after') or {}
    history = report.get('history_after') or {}
    db = connect()
    try:
        job = db.execute('select * from jobs where id=?', (JOB_ID,)).fetchone()
        attempts = db.execute('select * from submission_attempts where job_id=?', (JOB_ID,)).fetchall()
    finally:
        db.close()
    checks = {
        'exact_job_and_url': bool(job and job['requisition_id'] == REQ and job['canonical_url'] == URL),
        'same_single_locked_attempt': len(attempts) == 1 and attempts[0]['attempt_id'] == report.get('attempt_id')
            and attempts[0]['state'] == 'uncertain' and attempts[0]['proof_path'] is None,
        'no_completion_request': guard.get('completion_count') == 0 and not any(
            e.get('reason') == 'claimed_save_completion' for e in guard.get('events', [])),
        'no_commit': guard.get('workflow_committed') is False,
        'no_applied_history_match': history.get('full_history_loaded') is True
            and history.get('exact_id_matches') == 0 and history.get('company_role_matches') == 0,
        'listing_live_unapplied': listing.get('exact_path') is True and listing.get('apply_visible') is True
            and not listing.get('applied_button') and not listing.get('applied_status') and not listing.get('pending'),
        'current_location_blank': CURRENT_LOCATION_QUESTION in ui.get('prompts', [])
            and ui.get('editable_count') == 1 and ui.get('editable_filled') == [False]
            and not ui.get('thank_you'),
        'notice_answer_saved_once': report.get('notice_answer_save_count') == 1
            and report.get('save_clicked_by_agent') is True,
        'generic_profile_restored': report.get('generic_restored_verified') is True,
    }
    if not all(checks.values()):
        raise RuntimeError('Current-state evidence gate failed: ' + ', '.join(k for k, v in checks.items() if not v))
    history_at = history.get('checked_at')
    if not history_at:
        raise RuntimeError('Recent full Applied Jobs check has no timestamp')
    age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(history_at.replace('Z', '+00:00'))).total_seconds()
    if age < 0 or age > 900:
        raise RuntimeError('Recent full Applied Jobs check is stale')
    if not ROLE.is_file() or not GENERIC.is_file():
        raise RuntimeError('Role or generic PDF is missing')
    visual = json.loads((FOLDER / 'resume_visual_review.json').read_text())
    if visual.get('status') != 'visually_clean' or visual.get('pdf_sha256') != sha(ROLE):
        raise RuntimeError('Role PDF does not match its visual review')
    return report, job, attempts[0], checks


def confirmed_answer(question, profile):
    norm = re.sub(r'[^a-z0-9]+', ' ', question.lower()).strip()
    plain = re.sub(r'[*_`]', '', profile)
    if 'current location' in norm:
        match = re.search(r'^- Current location:\s*(.+)$', plain, re.M | re.I)
        if match and match.group(1).strip().lower() not in ('tbd', 'unknown', 'not confirmed'):
            return match.group(1).strip(), 'APPLICATION_PROFILE.md: confirmed current location'
    if any(key in norm for key in ('willing to relocate', 'open to relocation', 'preferred location')):
        if re.search(r'Open to: any work mode.*any city in India; willing to relocate', plain, re.I):
            return 'Open to any city in India; willing to relocate.', 'APPLICATION_PROFILE.md: confirmed location flexibility'
    if 'what is your total work experience' in norm and re.search(r'2\+\s+years overall\s*,\s*approximately', plain, re.I):
        return '2+ years', 'APPLICATION_PROFILE.md: approximate total professional experience'
    if 'notice period' in norm or 'last working day' in norm or 'joining date' in norm:
        if 'Last working day: 30 September 2026' in plain and 'Earliest joining date: 1 October 2026' in plain:
            return 'My last working day is 30 September 2026. I am available to join from 1 October 2026.', 'APPLICATION_PROFILE.md: confirmed last-working and earliest-joining dates'
    return None, None


def main():
    prior, job, attempt, checks = preflight()
    config_text = (ROOT / 'config/job_search.yaml').read_text()
    if 'submissions:\n  enabled: false' not in config_text:
        raise RuntimeError('Scheduled submissions are not disabled')
    profile_path = ROOT / 'APPLICATION_PROFILE.md'
    profile = profile_path.read_text()
    norm_question = lambda x: re.sub(r'[^a-z0-9]+', ' ', x.lower()).strip()
    already_answered = {
        norm_question('What is your total work experience?'),
        norm_question('What is your notice period,if serving notice period last working day?'),
    }
    result = {'job_id': JOB_ID, 'requisition_id': REQ, 'attempt_id': attempt['attempt_id'],
        'started_at': now(), 'precondition_checks': checks, 'new_attempt_created': False,
        'prior_answers_repeated': False, 'question_saves_in_this_continuation': 0,
        'outcome': 'Continuation started', 'network_responses': []}
    page = None
    guard = None
    route_guard = None
    try:
        with _chrome(headless=False) as context:
            page = context.pages[0] if context.pages else context.new_page()
            if not _status(page)['signed_in'] or not page.evaluate("()=>/Yugal\\s+Teotia/i.test(document.body.innerText)"):
                raise RuntimeError('Isolated signed-in Yugal account not verified')
            starting_resume = current_filename(page)
            result['profile_resume_observed_at_start'] = starting_resume
            if starting_resume != GENERIC.name:
                reset = upload_profile(page, GENERIC)
                if reset['after'] != GENERIC.name:
                    raise RuntimeError('Could not reestablish the generic profile resume before continuation')
                result['generic_reestablished_before_continuation'] = True
            result['signed_in_account_verified'] = True
            upload = upload_profile(page, ROLE)
            if upload['after'] != ROLE.name:
                raise RuntimeError('Role-specific resume is not active before continuing')
            result['role_resume_verified'] = True
            result['profile_upload_statuses'] = upload['response_statuses']

            listing = exact_listing(page)
            result['live_listing_before'] = listing
            result['applied_history_before'] = {
                'checked_at': json.loads(PRIOR.read_text())['history_after']['checked_at'],
                'full_history_loaded': True, 'exact_id_matches': 0, 'company_role_matches': 0,
                'reused_after_prior_question_save': True}
            if (not listing['exact_path'] or not listing['apply_visible'] or listing['applied_button']
                    or listing['applied_status'] or listing['pending']):
                raise RuntimeError('Fresh exact listing check failed')
            # history_snapshot navigates to Applied Jobs; return to the exact
            # requisition before resolving or activating its visible Apply control.
            listing = exact_listing(page)
            if (not listing['exact_path'] or not listing['apply_visible'] or listing['applied_button']
                    or listing['applied_status'] or listing['pending']):
                raise RuntimeError('Exact live listing changed after Applied Jobs verification')

            guard = NaukriPhaseGuard(REQ)
            pending = []

            def on_response(response):
                path = urlsplit(response.url).path
                if path not in (APPLY_PATH, CHAT_PATH):
                    return
                event = next((e for e in pending if e['path'] == path and e['response_status'] is None), None)
                metadata = {}
                try:
                    body = response.json()
                    node = body.get('chatbotResponse', {}) if isinstance(body, dict) else {}
                    metadata = {k: node.get(k) for k in ('isApply', 'isLeafNode', 'dataCommitted')
                        if isinstance(node.get(k), bool)}
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
                req = route.request
                path = urlsplit(req.url).path
                if req.method in ('GET', 'HEAD', 'OPTIONS'):
                    route.continue_()
                    return
                try:
                    payload = req.post_data_json if path in (APPLY_PATH, CHAT_PATH) else None
                except Exception:
                    payload = None
                allowed, event = guard.decide(req.method, req.url, payload)
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
            # Use a strict DOM predicate; stop unless exactly one visible exact-label Apply exists.
            controls = page.evaluate("""()=>[...document.querySelectorAll('#apply-button')]
                .filter(e=>e.getClientRects().length && e.innerText.trim()==='Apply').length""")
            if controls != 1:
                result['exact_visible_apply_count'] = controls
                result['outcome'] = 'Stopped: exact visible Apply control is not unique'
                return
            page.bring_to_front()
            page.evaluate("""()=>{const e=[...document.querySelectorAll('#apply-button')]
                .find(x=>x.getClientRects().length&&x.innerText.trim()==='Apply');e.click()}""")
            result['same_listing_reopened_by_agent'] = True
            page.wait_for_timeout(2500)
            ui = chat_snapshot(page)
            result['questionnaire_after_reopen'] = {'drawer': ui['drawer'], 'prompts': ui['prompts'],
                'editable_count': ui['editable_count'], 'editable_filled': ui['editable_filled'],
                'save_controls': ui['save_controls'], 'thank_you': ui['thank_you']}
            if guard.phase != 'chat_ready' or not ui['drawer'] or ui['thank_you']:
                result['outcome'] = 'Stopped: exact questionnaire state was not returned'
                result['network_guard'] = guard.summary()
                return
            guard.claim(attempt['attempt_id'])

            prior_questions = set(already_answered)
            current_prompt = latest_question(ui)
            processed = 0
            while processed < 10:
                current_norm = norm_question(current_prompt)
                if current_norm in prior_questions:
                    result['outcome'] = 'Stopped: portal returned an already-saved question; answer not repeated'
                    result['repeated_question_detected'] = True
                    break
                answer, fact_ref = confirmed_answer(current_prompt, profile)
                if not answer:
                    result['outcome'] = 'Blocked on a genuinely unknown required question; same attempt remains locked'
                    result['blocked_question'] = current_prompt
                    break
                if ui['editable_count'] != 1 or ui['editable_filled'] != [False]:
                    result['outcome'] = 'Stopped: current answer field is not blank and unique'
                    break
                editor = page.locator('.chatbot_Drawer [contenteditable=true]')
                editor.fill(answer, timeout=5000)
                if editor.inner_text().strip() != answer:
                    raise RuntimeError('Confirmed answer did not remain in the current visible editor')
                save = page.locator('.chatbot_Drawer .sendMsgbtn_container')
                if save.count() != 1 or save.inner_text().strip() != 'Save' or 'disabled' in (save.get_attribute('class') or ''):
                    raise RuntimeError('Current questionnaire Save control is not uniquely enabled')
                result.setdefault('answer_gate', []).append({'question_index': processed + 1, 'answer_ready': True})
                guard.arm_save(action_label='Save', exact_chat=True,
                    confirmed_answer=editor.inner_text().strip() == answer)
                save.click(timeout=10000)
                processed += 1
                result['question_saves_in_this_continuation'] = processed
                prior_questions.add(current_norm)
                result['answer_fact_refs'] = result.get('answer_fact_refs', []) + [fact_ref]
                result['last_prompt'] = current_prompt
                page.wait_for_timeout(2500)
                next_ui = chat_snapshot(page)
                result['questionnaire_after_last_save'] = {'drawer': next_ui['drawer'],
                    'prompts': next_ui['prompts'], 'editable_count': next_ui['editable_count'],
                    'editable_filled': next_ui['editable_filled'], 'thank_you': next_ui['thank_you'],
                    'applied_visible': next_ui['applied_visible']}
                if guard.workflow_committed or next_ui['thank_you']:
                    ui = next_ui
                    result['outcome'] = 'Workflow completion indication observed; checking exact submission proof'
                    break
                next_prompt = latest_question(next_ui)
                if not next_prompt or norm_question(next_prompt) == current_norm:
                    ui = next_ui
                    result['outcome'] = 'No distinct next question or completion indication; same attempt remains locked'
                    break
                prior_chat = next((e for e in reversed(guard.events) if e['reason'] == 'one_claimed_chat_answer'), None)
                next_save = page.locator('.chatbot_Drawer .sendMsgbtn_container')
                next_editor = page.locator('.chatbot_Drawer [contenteditable=true]')
                if (not prior_chat or not isinstance(prior_chat.get('response_status'), int)
                        or not 200 <= prior_chat['response_status'] < 300
                        or next_editor.count() != 1 or next_editor.inner_text().strip()
                        or next_save.count() != 1 or not next_save.is_visible()
                        or next_save.inner_text().strip() != 'Save'):
                    ui = next_ui
                    result['outcome'] = 'Stopped: next question state was not safely verified'
                    break
                # The next Save is permitted only for a distinct, blank, visibly enabled question.
                guard.acknowledge_next_question(question_changed=True, editor_empty=True, save_visible=True)
                ui = next_ui
                current_prompt = next_prompt

            result['network_guard'] = guard.summary()
            completion = [e for e in guard.events if e['reason'] == 'claimed_save_completion']
            network_ok = bool(completion and isinstance(completion[-1]['response_status'], int)
                and 200 <= completion[-1]['response_status'] < 300)
            positive_listing = False
            positive_history = False
            if network_ok or guard.completion_count or (result.get('questionnaire_after_last_save') or {}).get('thank_you'):
                listing_after = exact_listing(page)
                result['listing_after'] = listing_after
                positive_listing = listing_after['exact_path'] and (listing_after['applied_button'] or listing_after['applied_status'])
                if not positive_listing:
                    history_after = history_snapshot(page)
                    result['history_after'] = history_after
                    positive_history = history_after.get('full_history_loaded') is True and history_after['exact_id_matches'] > 0
            if network_ok and (positive_listing or positive_history):
                captured = now()
                proof = {'attempt_id': attempt['attempt_id'], 'requisition_id': REQ, 'captured_at': captured,
                    'url': URL if positive_listing else 'https://www.naukri.com/myapply/historypage',
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
            elif guard.completion_count or (result.get('questionnaire_after_last_save') or {}).get('thank_you'):
                result['outcome'] = 'Uncertain: same attempt remains locked; no retry without positive job-bound proof'
    except Exception as exc:
        result['error_type'] = type(exc).__name__
        result['error_message'] = str(exc)
        result['outcome'] = 'Stopped with existing attempt ID; no new attempt created'
    finally:
        if page is not None:
            try:
                with _chrome(headless=False) as restore_context:
                    restore_page = restore_context.pages[0] if restore_context.pages else restore_context.new_page()
                    if not _status(restore_page)['signed_in'] or not restore_page.evaluate("()=>/Yugal\\s+Teotia/i.test(document.body.innerText)"):
                        raise RuntimeError('Isolated account identity failed during resume restoration')
                    restored = upload_profile(restore_page, GENERIC)
                    result['generic_restored_verified'] = restored['after'] == GENERIC.name and current_filename(restore_page) == GENERIC.name
                    result['generic_restore_statuses'] = restored['response_statuses']
            except Exception as exc:
                result['generic_restored_verified'] = False
                result['generic_restore_error_type'] = type(exc).__name__
                result['generic_restore_error_message'] = str(exc)
        result['finished_at'] = now()
        RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    regenerate()
    print(json.dumps({'job_id': JOB_ID, 'requisition_id': REQ, 'attempt_id': attempt['attempt_id'],
        'outcome': result.get('outcome'), 'questions_saved_this_continuation': result.get('question_saves_in_this_continuation'),
        'completion_response_2xx': any(e.get('reason') == 'claimed_save_completion'
            and isinstance(e.get('response_status'), int) and 200 <= e['response_status'] < 300
            for e in (result.get('network_guard') or {}).get('events', [])),
        'generic_restored_verified': result.get('generic_restored_verified'),
        'blocked_question': result.get('blocked_question'), 'result_path': str(RESULT.relative_to(ROOT)),
        'confirmation_path': result.get('confirmation_path')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
