"""Contact regressions use synthetic controls; no browser or application actions."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import confirmed_forms as forms
from local_paths import ROOT

class LinkedInPhone(unittest.TestCase):
    def field(self,label='Phone',typ='text',options=None):
        return {'index':2,'control_id':'fixture-phone-nationalNumber','question':label,
                'type':typ,'required':True,'options':options or []}
    def identity(self):
        return {'identity':{'phone':'+91 98765 43210','email':'fixture@example.invalid',
                            'name':'Test Candidate','location':'Pune, India'},'answers':{'rules':[]}}
    def test_saved_phone_shape_maps_confirmed_private_fact_without_logging(self):
        saved=json.loads((ROOT/'jobs/linkedin-4478450952/linkedin-easy-apply-progress.json').read_text())['pending_field']
        self.assertEqual(saved['question'],'Phone')
        self.assertTrue(saved['control_id'].endswith('phoneNumber-nationalNumber'))
        # Assertions expose booleans only if the real private value is malformed.
        value,fact=forms.answer(saved,'')
        self.assertTrue(bool(value) and bool(forms.re.fullmatch(r'[6-9][0-9]{9}',value)))
        self.assertEqual(fact,'confirmed_identity:phone')
    def test_phone_aliases_and_separate_country_code(self):
        with patch.object(forms.candidate,'load',return_value=self.identity()):
            for label in ['Phone','Phone *','Phone (Required)','Mobile phone number','Phone number']:
                self.assertEqual(forms.answer(self.field(label),'')[0],'9876543210')
            self.assertEqual(forms.answer(self.field('Phone country code','select-one'),'')[0],'India (+91)')
            self.assertIsNone(forms.answer(self.field('Years of phone support experience'),'')[0])
    def test_exact_input_readback_accepts_separators_rejects_changed_number(self):
        scope=MagicMock();loc=scope.locator.return_value;field=self.field()
        loc.input_value.return_value='98765 43210'
        self.assertTrue(forms.fill(scope,field,'9876543210'))
        loc.fill.assert_called_once_with('9876543210')
        loc.fill.reset_mock()
        self.assertTrue(forms.fill(scope,field,'9876543210',verify_only=True))
        loc.fill.assert_not_called()
        for retained in ['9876543211','+91 9876543210','number: 9876543210','']:
            loc.input_value.return_value=retained
            self.assertFalse(forms.fill(scope,field,'9876543210',verify_only=True))
    def test_country_code_selection_and_readback_fail_closed(self):
        opts=[{'label':'India (+91)','value':'IN'}, {'label':'United States (+1)','value':'US'}]
        field=self.field('Phone country code','select-one',opts)
        scope=MagicMock();loc=scope.locator.return_value;loc.input_value.return_value='IN'
        self.assertTrue(forms.fill(scope,field,'India (+91)'))
        loc.select_option.assert_called_once_with(value='IN')
        loc.input_value.return_value='US'
        self.assertFalse(forms.fill(scope,field,'India (+91)',verify_only=True))
        loc.select_option.reset_mock()
        field['options']=[{'label':'Not listed','value':'unknown'}]
        self.assertFalse(forms.fill(scope,field,'India (+91)'))
        loc.select_option.assert_not_called()
    def test_invalid_private_phone_never_guesses(self):
        for raw in ['+1 9876543210','+91 123','9876543210']:
            profile=self.identity();profile['identity']['phone']=raw
            with patch.object(forms.candidate,'load',return_value=profile):
                self.assertEqual(forms.answer(self.field(),''),(None,None))
                self.assertEqual(forms.answer(self.field('Phone country code','select-one'),''),(None,None))

if __name__=='__main__':unittest.main()
