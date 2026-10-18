import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from browser_probe import identity_proof

class ProbeProofTests(unittest.TestCase):
    def test_both_actual_booleans_required(self):
        self.assertTrue(identity_proof({'content':[{'text':'### Result\n{"signed_in":true,"identity_match":true}'}]}))
        self.assertFalse(identity_proof({'signed_in':False,'identity_match':True}))
        self.assertFalse(identity_proof({'signed_in':'true','identity_match':'true'}))
        self.assertFalse(identity_proof('signed_in identity_match true'))

if __name__=='__main__': unittest.main()
