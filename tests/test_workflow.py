import sys, tempfile, unittest, json, os
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import workflow as w
import openpyxl

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.old=(w.ROOT,w.STATE,w.MASTER)
        w.ROOT=self.root;w.STATE=self.root/'.workflow'
        w.MASTER=self.old[2]
        for file in ['APPLICATION_PROFILE.md','SEARCH_PROFILE.md','WORKFLOW_AGENT.md']:
            (self.root/file).write_text((self.old[0]/file).read_text())
        wb=openpyxl.Workbook();ws=wb.active;ws.title='Tracker'
        ws.append(['Company','Role','Job ID','Direct link','Status','Confirmation evidence','Responses','Next step','Location','Submission date','Source'])
        wb.create_sheet('Guide')['A1']='preserved';wb.save(self.root/'tracker.xlsx')
    def tearDown(self):
        w.ROOT,w.STATE,w.MASTER=self.old;self.temp.cleanup()
    def test_alias_requisition_dedupe_and_backup(self):
        w.upsert({'Company':'Optum','Role':'Backend','Job ID':'123','Status':'Shortlisted'},'test')
        w.upsert({'Company':'UnitedHealth Group (Optum)','Role':'Renamed role','Job ID':'123','Status':'Shortlisted'},'test')
        wb,_,_,rows=w.tracker_rows();self.assertEqual(len(rows),1)
        self.assertEqual(wb['Guide']['A1'].value,'preserved')
        self.assertEqual(len(list((w.STATE/'changes').glob('*.json'))),2)
    def test_applied_needs_evidence_and_no_regression(self):
        with self.assertRaises(ValueError):w.upsert({'Company':'A','Role':'B','Job ID':'1','Status':'Applied'},'test')
        w.upsert({'Company':'A','Role':'B','Job ID':'1','Status':'Applied','Confirmation evidence':'proof'},'test')
        with self.assertRaises(ValueError):w.upsert({'Company':'A','Role':'B','Job ID':'1','Status':'Shortlisted'},'test')
    def test_pause_blocks_writes(self):
        w.write_json(w.STATE/'control.json',{'paused':True})
        with self.assertRaises(ValueError):w.upsert({'Company':'A','Role':'B'},'test')
    def test_scoped_confirmed_submission_and_response_transition(self):
        w.upsert({'Company':'Optum','Role':'Engineer','Job ID':'2388999','Status':'Stopped'},'test')
        w.write_json(w.STATE/'optum-2388999-authorization.json',{'batch_confirmed':True,'submission_authorized_after_confirmed_batch':True})
        w.write_json(w.STATE/'control.json',{'paused':True})
        row={'Company':'Optum','Role':'Engineer','Job ID':'2388999'}
        proof=self.root/'proof.md';proof.write_text('Process completed; No longer under consideration')
        w.upsert({**row,'Status':'Submission unconfirmed'},'authorized attempt',allow_paused=True,authorized_transition=True)
        w.upsert({**row,'Status':'Applied','Confirmation evidence':'proof.md','Submission date':'2026-09-28'},'confirmed',allow_paused=True,authorized_transition=True)
        import tracker_views as tv
        tv.views(w)
        self.assertEqual(w.read_json(self.root/'views/quick.json')[0]['Applied date'],'2026-09-28')
        w.upsert({**row,'Status':'Rejected','Confirmation evidence':'proof.md'},'portal response',allow_paused=True,authorized_transition=True)
        with self.assertRaises(ValueError):w.upsert({**row,'Status':'Applied','Confirmation evidence':'proof.md'},'no retry',allow_paused=True,authorized_transition=True)
        self.assertEqual(w.tracker_rows()[3][0]['Status'],'Rejected')
    def test_profile_conflicts_not_confirmed(self):
        a=w.audit()['answers']
        for field in ['citizenship','contractual_notice_period','java_professional_experience','total_experience']:
            self.assertEqual(a[field]['state'],'uncertain')
        self.assertEqual(a['joining_date']['state'],'confirmed')
        self.assertEqual(a['reason_for_leaving']['state'],'confirmed')
    def test_required_gap_prevents_high(self):
        p={'original_verified':True,'live':True,'india':True,'development_role':True,'requirements':[{'name':'Cloud','required':True,'basis':'gap','evidence':'No evidence'}]}
        self.assertNotEqual(w.score(p)[0],'High')
    def test_mail_exact_ambiguous_and_idempotency(self):
        w.upsert({'Company':'Optum','Role':'B','Job ID':'123','Status':'Shortlisted'},'test')
        p=self.root/'mail.json';w.write_json(p,[{'message_id':'m1','date':'2026-09-27','subject':'Application 123','company':'UnitedHealth Group','kind':'acknowledgment'},{'message_id':'m2','date':'2026-09-27','subject':'Hello candidate','company':'Optum'}])
        result=w.match_mail(p);self.assertEqual([r['match'] for r in result],['exact','review'])
        self.assertEqual(w.match_mail(p),[])
        self.assertEqual(w.tracker_rows()[3][0]['Status'],'Shortlisted')
    def test_formula_injection_saved_as_text(self):
        w.upsert({'Company':'A','Role':'=1+1','Job ID':'1'},'test')
        self.assertEqual(w.tracker_rows()[1]['B2'].data_type,'s')
    def test_stale_listing_and_unsafe_slug_blocked(self):
        with self.assertRaises(ValueError):w.fresh('2020-01-01T00:00:00Z')
        with self.assertRaises(ValueError):w.job_folder('../../outside')
    def test_form_blocks_unknown_and_omits_optional_salary(self):
        folder=w.job_folder('test');folder.mkdir(parents=True)
        w.write_json(folder/'listing.json',{'fit':'High','checked_at':w.now(),'company':'A','role':'B','job_id':'1','url':'https://example.com/jobs/1'})
        w.upsert({'Company':'A','Role':'B','Job ID':'1','Status':'Shortlisted'},'test')
        schema=self.root/'schema.json';w.write_json(schema,{'url':'https://example.com/form','fields':[{'label':'Name','answer_key':'name','required':True},{'label':'Citizenship','answer_key':'citizenship','required':True},{'label':'Salary','answer_key':'expected_ctc','required':False}]})
        with patch.object(w,'verify_visual_review',return_value={}):
            result=w.prepare('test',schema)
        self.assertEqual(result['blocked'],['Citizenship'])
        self.assertEqual([f['label'] for f in result['fields']],['Name'])
        self.assertFalse(result['live_form_filled'])
    def test_date_aware_notice_and_leaving_reason(self):
        import datetime as dt
        before=w.audit(dt.date(2026,9,28))['answers']
        after=w.audit(dt.date(2026,10,1))['answers']
        self.assertEqual(before['remaining_notice_days']['value'],3)
        self.assertEqual(after['remaining_notice_days']['value'],0)
        self.assertIn('ends on 30 September',before['reason_for_leaving']['value'])
        self.assertIn('ended,',after['reason_for_leaving']['value'])
    def test_missing_visual_review_blocks(self):
        folder=w.job_folder('test');folder.mkdir(parents=True)
        with self.assertRaises(ValueError):w.verify_visual_review(folder)
    def test_user_dates_calculate_professional_experience_only(self):
        import datetime as dt
        w.write_json(self.root/'employment_dates.json',w.read_json(self.old[0]/'employment_dates.json'))
        a=w.audit(dt.date(2026,9,28))['answers']
        self.assertIsNone(a['total_experience']['value'])
        self.assertEqual(a['total_experience']['state'],'uncertain')
        self.assertEqual(a['java_professional_experience']['value']['completed_months'],23)
        self.assertIsNone(a['datanova_employment_dates']['value'])
        self.assertEqual(a['datanova_employment_dates']['state'],'uncertain')
        self.assertEqual(a['datanova_employment_months']['value'],{'start':'2023-02','end':'2023-05'})
        self.assertEqual(w.audit(dt.date(2026,12,1))['answers']['total_experience']['value'],w.audit(dt.date(2026,10,1))['answers']['total_experience']['value'])
    def test_views_and_conservative_backfill(self):
        import tracker_views as tv
        w.upsert({'Company':'Optum','Role':'Engineer','Job ID':'123','Status':'Applied','Confirmation evidence':'proof'},'test')
        w.upsert({'Company':'Other','Role':'Engineer','Job ID':'456','Status':'Stopped'},'test')
        result=tv.views(w)
        self.assertEqual(result['quick_rows'],1);self.assertEqual(result['detailed_rows'],2)
        evidence=self.root/'history.json';evidence.write_text('{}')
        path=self.root/'capture.json'
        base={'company':'New','role':'Engineer','requisition_id':'7','date':'2026-09-01','evidence':'history.json'}
        w.write_json(path,{'records':[{**base,'kind':'alert'},{**base,'kind':'portal_application','requisition_id':'8'},{**base,'kind':'portal_application','requisition_id':None},{**base,'kind':'portal_application','company':'Optum','requisition_id':'123'}]})
        result=tv.preview(w,path)
        self.assertEqual([p['decision'] for p in result['proposals']],['ignore','proposed','review','existing'])
        self.assertEqual(len(w.tracker_rows()[3]),2)
        approval=self.root/'approval.json';w.write_json(approval,{'approved_ids':[result['proposals'][2]['id']]})
        with self.assertRaises(ValueError):tv.import_reviewed(w,approval)
        import hashlib
        w.write_json(approval,{'preview_sha256':hashlib.sha256((w.STATE/'historical-preview.json').read_bytes()).hexdigest(),'approved_ids':[result['proposals'][1]['id']]})
        self.assertEqual(tv.import_reviewed(w,approval)['imported'],1)
        self.assertEqual(len(w.tracker_rows()[3]),3)
        with self.assertRaises(ValueError):tv.import_reviewed(w,approval)
    def test_quick_includes_requested_states_and_stopped_optum(self):
        import tracker_views as tv
        for i,status in enumerate(['Shortlisted','Prepared','Blocked','Applied','Excluded — closed','Stopped']):
            w.upsert({'Company':'Optum' if status=='Stopped' else 'Company'+str(i),'Role':'Engineer','Job ID':str(i),'Status':status,'Confirmation evidence':'proof' if status=='Applied' else None,'Next step':'Do not resume without authorization' if status=='Stopped' else 'Review next action'},'test')
        folder=w.job_folder('optum');folder.mkdir(parents=True)
        w.write_json(folder/'listing.json',{'company':'Optum','job_id':'5','role':'Engineer'})
        w.write_json(folder/'application_stopped.json',{'reason':'User stopped'})
        result=tv.views(w)
        self.assertEqual(result['quick_rows'],5)
        quick=w.read_json(self.root/'views/quick.json')
        optum=next(r for r in quick if r['Company']=='Optum')
        self.assertEqual(optum['Status'],'Blocked')
        self.assertIn('authorization',optum['Next action'])
        self.assertEqual(w.tracker_rows()[3][-1]['Status'],'Stopped')
    def test_browser_step_log_and_no_false_ready(self):
        folder=w.job_folder('test');folder.mkdir(parents=True)
        evidence=self.root/'evidence.txt';evidence.write_text('Login required')
        step=self.root/'step.json';w.write_json(step,{'action':'login_blocked','url':'https://example.com','captured_at':w.now(),'artifact':'evidence.txt'})
        w.browser_step('test',step);w.browser_step('test',step)
        self.assertEqual(len(w.read_json(folder/'browser_steps.json')),1)
        self.assertIn('browser_step',(w.STATE/'events.jsonl').read_text())
    def test_cli_launch_is_bounded_and_no_bypass_flags(self):
        import daily_runner as dr
        folder=self.root/'bin';folder.mkdir()
        fake=folder/'codex'
        fake.write_text('#!'+sys.executable+'\nimport sys,json\nrequest=sys.stdin.read()\nassert "Submission remains disabled" in request\nassert not any("bypass" in arg for arg in sys.argv)\nprint(json.dumps({"item":{"type":"agent_message","text":"SECRET_SENTINEL_DO_NOT_LOG"}}))\nprint(json.dumps({"type":"turn.completed"}))\n')
        fake.chmod(0o700)
        with patch.dict(os.environ,{'PATH':str(folder)+os.pathsep+os.environ['PATH']}),patch.object(dr,'ROOT',self.root),patch.object(dr,'STATE',self.root/'.workflow'),patch.object(dr,'gate'),patch.object(dr.subprocess,'run',return_value=type('Result',(),{'returncode':0})()):
            w.run('search',execute=True)
        output=next((w.STATE/'runs').glob('*.jsonl')).read_text()
        self.assertNotIn('SECRET_SENTINEL',output)
        self.assertIn('turn.completed',output)
        self.assertEqual(w.read_json(w.STATE/'active.json'),{})

if __name__=='__main__':unittest.main()
