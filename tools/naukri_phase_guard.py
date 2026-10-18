"""Fail-closed, job-bound network guard for one Naukri native-Apply attempt.

Only sanitized protocol metadata is retained. In particular, neither request
bodies nor response bodies, URLs with query strings, cookies, or answers are
written to the audit.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from urllib.parse import urlsplit

APPLY_PATH = '/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply'
CHAT_PATH = '/cloudgateway-chatbot/chatbot-services/botapi/v5/respond'
COMPLETION_FLOWTYPES = frozenset({'apply', 'submit', 'complete', 'save'})
MAX_REOPEN = 20
REOPEN_PHASES = frozenset({'answer_sent', 'followup_status_received', 'followup_status_sent',
                           'completion_received', 'post_completion_status_received'})
SAFE_VALUE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,31}$')


def _safe(value):
    return value if isinstance(value, str) and SAFE_VALUE.fullmatch(value) else 'unknown'


def _exact_job(packet, job_id):
    jobs = packet.get('strJobsarr')
    if isinstance(jobs, str):
        try:jobs = json.loads(jobs)
        except ValueError:return False
    if not isinstance(jobs, list) or len(jobs) != 1:
        return False
    item = jobs[0]
    if isinstance(item, str) and item.startswith('{'):
        try:item = json.loads(item)
        except ValueError:return False
    if isinstance(item, dict):
        ids = [str(item.get(key)) for key in ('jobId', 'jobid', 'jobID') if item.get(key) is not None]
        if ids:return all(value == job_id for value in ids)
        return str(item.get('id')) == job_id
    return str(item) == job_id


class NaukriPhaseGuard:
    def __init__(self, job_id: str):
        if not str(job_id).isdigit():
            raise ValueError('Naukri job ID must be numeric')
        self.job_id = str(job_id)
        self.phase = 'preflight'
        self.claimed_attempt_id = None
        self.save_armed = False
        self.show_count = 0
        self.answer_count = 0
        self.save_arm_count = 0
        self.followup_status_count = 0
        self.post_completion_status_count = 0
        self.completion_count = 0
        self.reopen_count = 0
        self.workflow_committed = False
        self.started = time.monotonic()
        self.events = []

    def claim(self, attempt_id):
        if self.phase=='claimed' and self.claimed_attempt_id==str(attempt_id):return
        if self.phase != 'chat_ready' or not attempt_id or self.claimed_attempt_id:
            raise RuntimeError('Cannot claim before exact chat opens')
        self.claimed_attempt_id = str(attempt_id)
        self.phase = 'claimed'

    def claim_before_open(self,attempt_id):
        """Register the one claim before Apply can complete a job with no questionnaire."""
        if self.phase!='preflight' or self.claimed_attempt_id or not attempt_id:
            raise RuntimeError('Initial native Apply claim is not ready')
        self.claimed_attempt_id=str(attempt_id)

    def arm_save(self, *, action_label, exact_chat, confirmed_answer):
        answer_ready = self.phase == 'claimed' or self.phase == 'followup_status_received'
        if (not answer_ready or action_label != 'Save' or not exact_chat or
                not confirmed_answer or self.save_arm_count >= 20 or
                self.save_arm_count != self.answer_count):
            raise RuntimeError('Save UI and confirmed answer are not bound to claimed job')
        self.save_arm_count += 1
        self.save_armed = True
        self.phase = 'save_armed'

    def may_reopen(self):
        """Naukri can pause a multi-question chat after a Save; the same claimed attempt may reopen it."""
        return bool(self.claimed_attempt_id and self.answer_count >= 1 and self.save_arm_count == self.answer_count
                    and not self.workflow_committed and self.completion_count == 0
                    and self.reopen_count < min(MAX_REOPEN,self.answer_count*2) and self.phase in REOPEN_PHASES)

    def arm_reopen(self):
        if not self.may_reopen():
            raise RuntimeError('Reopening the claimed questionnaire is not permitted')
        self.reopen_count += 1
        self.phase = 'reopen_armed'

    def acknowledge_next_question(self, *, question_changed, editor_empty, save_visible):
        """Advance only after the last confirmed answer succeeded and a new blank question is visible."""
        last_answer = next((event for event in reversed(self.events)
                            if event['reason'] == 'one_claimed_chat_answer'), None)
        if (self.phase not in ('answer_sent','followup_status_received') or not self.claimed_attempt_id or
                self.answer_count != self.save_arm_count or self.completion_count or
                self.workflow_committed or not last_answer or
                not isinstance(last_answer.get('response_status'), int) or
                not 200 <= last_answer['response_status'] < 300 or
                not question_changed or not editor_empty or not save_visible):
            raise RuntimeError('A distinct blank questionnaire question was not verified after the prior answer')
        self.phase = 'followup_status_received'
        self._event('visible_question_ready', 'UI', None, True, 'next_blank_job_question_after_saved_answer')

    def _event(self, path, method, flowtype, allowed, reason):
        item = {'at': dt.datetime.now(dt.timezone.utc).isoformat(),
                'elapsed_ms': round((time.monotonic() - self.started) * 1000),
                'job_id': self.job_id, 'phase': self.phase,
                'method': method, 'path': path, 'flowtype': _safe(flowtype),
                'allowed': allowed, 'reason': reason, 'response_status': None}
        self.events.append(item)
        return item

    def decide(self, method, url, packet):
        parsed=urlsplit(url);path=parsed.path
        if method in ('GET', 'HEAD', 'OPTIONS'):
            return True, None
        if path not in (APPLY_PATH, CHAT_PATH):
            if any(word in path.lower() for word in ('/apply-workflow/', '/botapi/', '/submit', '/upload', '/resume')):
                return False, self._event('unrecognized_application_endpoint', method, None, False,
                                          'unrecognized_application_endpoint')
            return True, None
        if parsed.scheme!='https' or parsed.hostname!='www.naukri.com':
            return False,self._event('untrusted_application_host',method,None,False,'untrusted_application_host')
        if not isinstance(packet, dict):
            return False, self._event(path, method, None, False, 'invalid_payload')
        flowtype = packet.get('flowtype') if path == APPLY_PATH else None
        if method != 'POST':
            return False, self._event(path, method, flowtype, False, 'invalid_method')
        if path == APPLY_PATH:
            bound = _exact_job(packet, self.job_id)
            if self.phase == 'preflight' and self.claimed_attempt_id and flowtype == 'show' and bound and self.show_count == 0:
                self.show_count += 1
                self.phase = 'show_sent'
                return True, self._event(path, method, flowtype, True, 'exact_job_show')
            if self.phase == 'reopen_armed' and flowtype == 'show' and bound and self.claimed_attempt_id:
                self.phase = 'reopen_show_sent'
                return True, self._event(path, method, flowtype, True, 'reopen_same_claimed_attempt_show')
            if (flowtype == 'show' and bound and self.claimed_attempt_id
                    and self.save_armed and self.answer_count > 0 and self.followup_status_count < self.answer_count*3
                    and self.phase in ('answer_sent', 'followup_status_received')):
                # Naukri refreshes the workflow state after the chat Save. This
                # is a read/status phase, not an application completion action.
                self.followup_status_count += 1
                self.phase = 'followup_status_sent'
                return True, self._event(path, method, flowtype, True, 'claimed_job_status_refresh')
            if (self.phase == 'completion_received' and flowtype == 'show' and bound
                    and self.claimed_attempt_id and self.post_completion_status_count == 0):
                self.post_completion_status_count += 1
                self.phase = 'post_completion_status_sent'
                return True, self._event(path, method, flowtype, True, 'post_completion_job_status')
            initial_leaf=(self.phase=='claimed' and self.answer_count==0 and any(
                e['reason']=='exact_job_show' and e.get('response_status')==200 and
                e.get('response_metadata',{}).get('isLeafNode') is True for e in self.events))
            if ((self.phase in ('answer_sent', 'followup_status_received') or initial_leaf) and self.claimed_attempt_id
                    and self.save_arm_count == self.answer_count and self.completion_count == 0 and bound
                    and not self.workflow_committed and flowtype in COMPLETION_FLOWTYPES):
                self.completion_count += 1
                self.phase = 'completion_sent'
                return True, self._event(path, method, flowtype, True, 'claimed_save_completion')
            return False, self._event(path, method, flowtype, False,
                                      'not_claimed_job_bound_completion')
        if (self.phase == 'save_armed' and self.claimed_attempt_id
                and self.answer_count < self.save_arm_count):
            # The caller must validate the answer against the visible, confirmed
            # question before calling this method; no answer text is retained.
            self.answer_count += 1
            self.phase = 'answer_sent'
            return True, self._event(path, method, None, True, 'one_claimed_chat_answer')
        return False, self._event(path, method, None, False, 'chat_answer_not_armed')

    def response(self, request_event, status, metadata=None):
        if request_event is not None and request_event['allowed']:
            request_event['response_status'] = int(status)
            request_event['response_elapsed_ms'] = round((time.monotonic() - self.started) * 1000)
            if isinstance(metadata, dict):
                safe_metadata = {}
                for key in ('isApply', 'isLeafNode', 'dataCommitted', 'job_id_match'):
                    if isinstance(metadata.get(key), bool):
                        safe_metadata[key] = metadata[key]
                if isinstance(metadata.get('currentNodeName'), str):
                    safe_metadata['currentNodeName'] = _safe(metadata['currentNodeName'])
                if safe_metadata:
                    request_event['response_metadata'] = safe_metadata
                if isinstance(metadata.get('dataCommitted'), bool) and metadata['dataCommitted']:
                    self.workflow_committed = True
            if request_event['reason'] == 'exact_job_show' and status == 200:
                self.phase = 'claimed' if self.claimed_attempt_id else 'chat_ready'
            elif request_event['reason'] == 'reopen_same_claimed_attempt_show':
                self.phase = 'followup_status_received' if status < 400 else 'followup_status_failed'
            elif request_event['reason'] == 'claimed_job_status_refresh':
                self.phase = 'followup_status_received' if status < 400 else 'followup_status_failed'
            elif request_event['reason'] == 'claimed_save_completion':
                self.phase = 'completion_received' if 200 <= status < 300 else 'completion_failed'
            elif request_event['reason'] == 'post_completion_job_status':
                self.phase = 'post_completion_status_received' if status < 400 else 'post_completion_status_failed'

    def summary(self):
        return {'job_id': self.job_id, 'phase': self.phase,
                'show_count': self.show_count, 'answer_count': self.answer_count,
                'save_arm_count': self.save_arm_count,
                'followup_status_count': self.followup_status_count,
                'post_completion_status_count': self.post_completion_status_count,
                'completion_count': self.completion_count,'reopen_count': self.reopen_count,
                'workflow_committed': self.workflow_committed,
                'events': list(self.events)}
