import sys,unittest,json,tempfile
from pathlib import Path
from unittest.mock import MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import naukri_native_worker as worker,auto_fit,candidate,job_config,confirmed_forms
from test_auto_pipeline import NEWGEN

class NativeControls(unittest.TestCase):
 def test_experience_is_checked_before_submission_and_month_units_are_respected(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'employment_dates.json').write_text(json.dumps({'employment':[{'company':'Synthetic','start':'2024-01-01','end':'2026-01-01','java':True}]}))
   field={'question':'Total number of years of experience in the industry?'}
   with patch.object(confirmed_forms,'ROOT',root):
    self.assertEqual(confirmed_forms.validate_answer(field,'5'),'candidate_experience_date_conflict')
    self.assertIsNone(confirmed_forms.validate_answer(field,'24 months'))
    self.assertIsNone(confirmed_forms.validate_answer(field,'2 years'))
 def test_integer_experience_uses_completed_years_never_ceiling(self):
  field={'question':'Years of Java experience?','type':'number','step':'1'}
  self.assertEqual(confirmed_forms.format_answer(field,'1.96'),'1')
  self.assertEqual(confirmed_forms.format_answer(field,'2.5'),'2')
  self.assertEqual(confirmed_forms.format_answer({**field,'step':'0.01'},'1.96'),'1.96')
 def test_hidden_radio_uses_label_and_resume_verifies_without_replaying(self):
  state={'yes':False,'no':True};scope=MagicMock();label=MagicMock();label.count.return_value=1
  def select(**kw):state.update(yes=True,no=False)
  label.click.side_effect=select
  def locator(selector):
   if selector.startswith('label'):return label
   control=MagicMock();control.is_checked.side_effect=lambda:state['yes' if 'yes' in selector else 'no'];return control
  scope.locator.side_effect=locator
  field={'index':0,'question':'Authorized?','type':'radio','options':[{'label':'Yes','value':'yes','index':0,'control_id':'yes'},{'label':'No','value':'no','index':1,'control_id':'no'}]}
  self.assertTrue(confirmed_forms.fill(scope,field,'Yes'));self.assertTrue(confirmed_forms.fill(scope,field,'Yes',verify_only=True))
  self.assertEqual(label.click.call_count,1)
 def test_checkbox_and_dropdown_bind_exact_confirmed_values(self):
  page=MagicMock();loc=MagicMock();page.locator.return_value=loc;loc.is_checked.return_value=True
  ui={'editor_count':0,'options':[{'text':'Java','id':'java','type':'checkbox','checked':False,'value':'j'}, {'text':'Kafka','id':'kafka','type':'checkbox','checked':False,'value':'k'}]}
  ok,payloads=worker._stage_answer(page,ui,['Java']);self.assertTrue(ok);self.assertEqual(payloads,[['Java'],['j']])
  self.assertEqual([x.args[0] for x in loc.set_checked.call_args_list],[True,False])
  loc.input_value.return_value='yes'
  ok,payloads=worker._stage_answer(page,{'selects':[{'options':[{'text':'Yes','value':'yes'}]}]},'Yes')
  self.assertTrue(ok);self.assertEqual(payloads,['Yes','yes']);loc.select_option.assert_called_once_with(value='yes')
  self.assertFalse(worker._payload_matches(['Java','Kafka'],['Java']))
 def test_question_keys_distinguish_server_steps_and_keep_same_step_stable(self):
  phase=MagicMock();phase.events=[{'response_metadata':{'currentNodeName':'one'}}]
  ui={'prompts':['Are you available?'],'options':[]}
  first=worker._question_key(ui,phase);self.assertEqual(first,worker._question_key(ui,phase))
  phase.events.append({'response_metadata':{'currentNodeName':'two'}});self.assertNotEqual(first,worker._question_key(ui,phase))
 def test_framework_neutral_and_senior_do_not_reject_but_mandatory_replacement_framework_does(self):
  cfg=job_config.load();cfg['high_fit']['secondary_skills_policy']='block';c=candidate.load()
  neutral=NEWGEN.replace('Spring Boot','Java REST APIs')
  p,reasons,_=auto_fit.build_packet({'jd_text':neutral,'title':'Senior Java Developer'},c,cfg)
  self.assertFalse(reasons);self.assertIsNotNone(p)
  p,reasons,_=auto_fit.build_packet({'jd_text':NEWGEN.replace('Spring Boot','Quarkus'),'title':'Java Developer'},c,cfg)
  self.assertFalse(reasons);self.assertTrue(any(x['name']=='other_frameworks' and x['basis']=='gap' and x['essential'] for x in p['requirements']))
 def test_java_is_not_hidden_by_optional_secondary_language(self):
  jd=NEWGEN.replace('using Spring Boot.','primarily in Java using Spring Boot, with exposure to Python or other languages.')
  p,reasons,_=auto_fit.build_packet({'jd_text':jd,'title':'Software Engineer'},candidate.load(),job_config.load())
  self.assertFalse(reasons);self.assertTrue(any(x['name']=='java' for x in p['requirements']))

if __name__=='__main__':unittest.main()
