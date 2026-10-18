import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import naukri_native_worker as worker
from application_budget import AttemptBudget

class StartupRepair(unittest.TestCase):
    def run_check(self,names,success=True):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        root=Path(temp.name);(root/'.workflow').mkdir();pdf=root/'Generic.pdf';pdf.write_bytes(b'confirmed generic fixture')
        sha=worker._sha(pdf);(root/'.workflow/naukri-live-verified.json').write_text(json.dumps({'generic_resume_restored':True,'generic_resume_sha256':sha}))
        page=MagicMock();path=root/'evidence.json'
        uploaded={'before':'Role.pdf','after':pdf.name,'pdf_sha256':sha,'response_statuses':[{'status':200 if success else 500}]}
        with patch.object(worker,'ROOT',root),patch.object(worker,'GENERIC',pdf),patch.object(worker,'current_filename',side_effect=names),patch.object(worker,'upload_profile',return_value=uploaded) as upload:
            if success:result=worker.ensure_generic_resume(page,{'id':1},'1234567890',path)
            else:
                with self.assertRaisesRegex(RuntimeError,'could not be verified'):worker.ensure_generic_resume(page,{'id':1},'1234567890',path)
                result=json.loads(path.read_text())
        return result,upload.call_count,(root/'.workflow/profile-restore-pending.json').exists()

    def test_delayed_filename_is_not_misclassified_as_changed_resume(self):
        result,calls,marker=self.run_check([None,None,'Generic.pdf','Generic.pdf','Generic.pdf'])
        self.assertTrue(result['verified']);self.assertFalse(result['repair_performed']);self.assertEqual(calls,0);self.assertFalse(marker)

    def test_actual_repair_has_hash_ack_and_filename_proof_then_can_continue(self):
        result,calls,marker=self.run_check(['Role.pdf','Role.pdf','Generic.pdf'])
        self.assertTrue(result['verified']);self.assertTrue(result['repair_performed']);self.assertEqual(calls,1);self.assertFalse(marker)
        self.assertEqual(result['upload']['pdf_sha256'],result['expected_sha256']);self.assertTrue(result['before_application_action'])

    def test_failed_repair_keeps_recovery_marker_and_uses_no_slot(self):
        result,calls,marker=self.run_check(['Role.pdf','Role.pdf'],success=False)
        self.assertFalse(result['verified']);self.assertTrue(marker)
        budget=AttemptBudget(10);out={'preflight_resume_repair_only':True,'status':'Blocked'}
        budget.record(out,None,None)
        self.assertEqual(budget.used,0);self.assertEqual(budget.new_claims,0);self.assertEqual(budget.continuations,0)
        self.assertFalse(out['new_attempt_slot_consumed'])

    def test_verified_repair_without_a_later_claim_also_uses_no_slot(self):
        budget=AttemptBudget(10);out={'generic_resume_repaired_at_start':True,'status':'Blocked'}
        budget.record(out,None,None);self.assertEqual(budget.used,0)
        self.assertEqual(budget.summary()['preflight_repairs_without_claim'],1)

if __name__=='__main__':unittest.main()
