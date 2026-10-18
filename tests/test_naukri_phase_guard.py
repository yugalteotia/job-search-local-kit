import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from naukri_phase_guard import APPLY_PATH, CHAT_PATH, NaukriPhaseGuard


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = NaukriPhaseGuard('1234567890')

    def show(self,attempt='attempt-one'):
        self.guard.claim_before_open(attempt)
        ok, event = self.guard.decide('POST', 'https://www.naukri.com' + APPLY_PATH,
                                      {'flowtype': 'show', 'strJobsarr': [{'jobId': '1234567890'}]})
        self.assertTrue(ok)
        self.guard.response(event, 200)

    def test_single_claimed_completion(self):
        self.show()
        self.guard.claim('attempt-one')
        self.guard.arm_save(action_label='Save', exact_chat=True, confirmed_answer=True)
        ok, event = self.guard.decide('POST', 'https://www.naukri.com' + CHAT_PATH,
                                      {'input': {'text': ['Yes'], 'token': 'SECRET'}})
        self.assertTrue(ok)
        self.guard.response(event, 200)
        ok, event = self.guard.decide('POST', 'https://www.naukri.com' + APPLY_PATH,
                                      {'flowtype': 'apply', 'strJobsarr': [{'jobId': '1234567890'}],
                                       'answer': 'PRIVATE', 'token': 'SECRET'})
        self.assertTrue(ok)
        self.guard.response(event, 200)
        again, _ = self.guard.decide('POST', 'https://www.naukri.com' + APPLY_PATH,
                                     {'flowtype': 'apply', 'strJobsarr': [{'jobId': '1234567890'}]})
        self.assertFalse(again)
        audit = json.dumps(self.guard.summary())
        self.assertNotIn('SECRET', audit)
        self.assertNotIn('PRIVATE', audit)
        self.assertNotIn('Yes', audit)
        self.assertEqual(self.guard.completion_count, 1)

    def test_unknown_and_other_job_fail_closed(self):
        self.show()
        self.guard.claim('attempt-one')
        self.guard.arm_save(action_label='Save', exact_chat=True, confirmed_answer=True)
        for flowtype, job in [('show', '9999999999'), ('apply', '9999999999'),
                              ('mystery', '1234567890')]:
            ok, _ = self.guard.decide('POST', 'https://www.naukri.com' + APPLY_PATH,
                                      {'flowtype': flowtype, 'strJobsarr': [{'jobId': job}]})
            self.assertFalse(ok)
        self.assertEqual(self.guard.completion_count, 0)

    def test_followup_show_is_job_bound_status_not_submission(self):
        self.show()
        self.guard.claim('attempt-one')
        self.guard.arm_save(action_label='Save', exact_chat=True, confirmed_answer=True)
        ok, answer = self.guard.decide('POST', 'https://www.naukri.com' + CHAT_PATH,
                                       {'input': {'text': ['Pune'], 'token': 'SECRET'}})
        self.assertTrue(ok)
        self.guard.response(answer, 200)
        ok, status = self.guard.decide('POST', 'https://www.naukri.com' + APPLY_PATH,
                                      {'flowtype': 'show', 'strJobsarr': [{'jobId': '1234567890'}]})
        self.assertTrue(ok)
        self.assertEqual(status['reason'], 'claimed_job_status_refresh')
        self.assertEqual(self.guard.completion_count, 0)
        self.guard.response(status, 200, {'isApply': True, 'dataCommitted': False,
                                          'currentNodeName': 'apply3', 'job_id_match': True,
                                          'answer': 'PRIVATE', 'token': 'SECRET'})
        self.assertEqual(self.guard.phase, 'followup_status_received')
        ok, complete = self.guard.decide('POST', 'https://www.naukri.com' + APPLY_PATH,
                                         {'flowtype': 'apply', 'strJobsarr': [{'jobId': '1234567890'}]})
        self.assertTrue(ok)
        self.guard.response(complete, 200)
        self.assertEqual(self.guard.completion_count, 1)
        again, _ = self.guard.decide('POST', 'https://www.naukri.com' + APPLY_PATH,
                                     {'flowtype': 'apply', 'strJobsarr': [{'jobId': '1234567890'}]})
        extra_status, status = self.guard.decide('POST', 'https://www.naukri.com' + APPLY_PATH,
                                                 {'flowtype': 'show', 'strJobsarr': [{'jobId': '1234567890'}]})
        self.assertFalse(again)
        self.assertTrue(extra_status)
        self.assertEqual(status['reason'], 'post_completion_job_status')
        self.guard.response(status, 200, {'job_id_match': True})
        duplicate_status, _ = self.guard.decide('POST', 'https://www.naukri.com' + APPLY_PATH,
                                                {'flowtype': 'show', 'strJobsarr': [{'jobId': '1234567890'}]})
        self.assertFalse(duplicate_status)
        audit = json.dumps(self.guard.summary())
        self.assertNotIn('SECRET', audit)
        self.assertNotIn('PRIVATE', audit)
        self.assertNotIn('Pune', audit)
        self.assertNotIn('Yes', audit)

    def test_two_confirmed_questions_allow_status_refreshes_but_one_completion(self):
        self.show('attempt-two');self.guard.claim('attempt-two')
        for answer in ('Pune', 'Yes'):
            self.guard.arm_save(action_label='Save', exact_chat=True, confirmed_answer=True)
            ok, event = self.guard.decide('POST', 'https://www.naukri.com' + CHAT_PATH,
                                          {'input': {'text': [answer], 'token': 'SECRET'}})
            self.assertTrue(ok);self.guard.response(event, 200)
            ok, event = self.guard.decide('POST', 'https://www.naukri.com' + APPLY_PATH,
                                          {'flowtype': 'show', 'strJobsarr': [{'jobId': '1234567890'}]})
            self.assertTrue(ok);self.guard.response(event, 200, {'job_id_match': True, 'dataCommitted': False})
        ok, completion = self.guard.decide('POST', 'https://www.naukri.com' + APPLY_PATH,
                                           {'flowtype': 'apply', 'strJobsarr': [{'jobId': '1234567890'}]})
        self.assertTrue(ok);self.guard.response(completion, 200)
        duplicate, _ = self.guard.decide('POST', 'https://www.naukri.com' + APPLY_PATH,
                                         {'flowtype': 'apply', 'strJobsarr': [{'jobId': '1234567890'}]})
        self.assertFalse(duplicate)
        summary=json.dumps(self.guard.summary())
        self.assertEqual(self.guard.answer_count,2)
        self.assertEqual(self.guard.followup_status_count,2)
        self.assertEqual(self.guard.completion_count,1)
        self.assertNotIn('SECRET',summary);self.assertNotIn('Pune',summary);self.assertNotIn('Yes',summary)

    def test_ui_advanced_to_distinct_blank_question_allows_same_attempt_answer(self):
        self.show('same-attempt');self.guard.claim('same-attempt')
        self.guard.arm_save(action_label='Save',exact_chat=True,confirmed_answer=True)
        ok, first=self.guard.decide('POST','https://www.naukri.com'+CHAT_PATH,{'input':{'text':['Pune']}})
        self.assertTrue(ok);self.guard.response(first,200)
        with self.assertRaises(RuntimeError):
            self.guard.acknowledge_next_question(question_changed=False,editor_empty=True,save_visible=True)
        self.guard.acknowledge_next_question(question_changed=True,editor_empty=True,save_visible=True)
        self.guard.arm_save(action_label='Save',exact_chat=True,confirmed_answer=True)
        ok, second=self.guard.decide('POST','https://www.naukri.com'+CHAT_PATH,{'input':{'text':['Yes']}})
        self.assertTrue(ok);self.guard.response(second,200)
        ok, complete=self.guard.decide('POST','https://www.naukri.com'+APPLY_PATH,
            {'flowtype':'apply','strJobsarr':[{'jobId':'1234567890'}]})
        self.assertTrue(ok);self.guard.response(complete,200)
        self.assertEqual(self.guard.answer_count,2)
        self.assertEqual(self.guard.completion_count,1)
        audit=json.dumps(self.guard.summary())
        self.assertNotIn('Pune',audit);self.assertNotIn('Yes',audit)

    def test_committed_status_cannot_be_followed_by_completion(self):
        self.show()
        self.guard.claim('attempt-one')
        self.guard.arm_save(action_label='Save', exact_chat=True, confirmed_answer=True)
        ok, answer = self.guard.decide('POST', 'https://www.naukri.com' + CHAT_PATH,
                                       {'input': {'text': ['Yes']}})
        self.assertTrue(ok)
        self.guard.response(answer, 200)
        ok, status = self.guard.decide('POST', 'https://www.naukri.com' + APPLY_PATH,
                                      {'flowtype': 'show', 'strJobsarr': [{'jobId': '1234567890'}]})
        self.assertTrue(ok)
        self.guard.response(status, 200, {'dataCommitted': True})
        duplicate, _ = self.guard.decide('POST', 'https://www.naukri.com' + APPLY_PATH,
                                        {'flowtype': 'apply', 'strJobsarr': [{'jobId': '1234567890'}]})
        self.assertFalse(duplicate)
        self.assertTrue(self.guard.summary()['workflow_committed'])

    def test_requires_claim_and_save(self):
        self.show()
        ok, _ = self.guard.decide('POST', 'https://www.naukri.com' + APPLY_PATH,
                                  {'flowtype': 'apply', 'strJobsarr': [{'jobId': '1234567890'}]})
        self.assertFalse(ok)
        self.guard.claim('attempt-one')
        with self.assertRaises(RuntimeError):
            self.guard.arm_save(action_label='Submit', exact_chat=True, confirmed_answer=True)

    def test_token_bearing_urls_are_never_recorded(self):
        ok, _ = self.guard.decide('POST',
            'https://www.naukri.com/cloudgateway-workflow/private/submit/SECRET?token=SECRET',
            {'token': 'SECRET'})
        self.assertFalse(ok)
        self.assertNotIn('SECRET', json.dumps(self.guard.summary()))


if __name__ == '__main__':
    unittest.main()
