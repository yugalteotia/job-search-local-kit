import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import candidate
PROFILE=(candidate.ROOT/'APPLICATION_PROFILE.md').read_text()
class CombinedRelocation(unittest.TestCase):
    def test_or_uses_confirmed_willingness_without_claiming_residence(self):
        for q in ['Are you currently living in or ready to relocate to Bengaluru ?',
                  'Are you currently residing in Pune or willing to relocate to Bangalore?',
                  'Are you currently based in Bengaluru or willing to relocate there?']:
            self.assertEqual(candidate.answer(q,PROFILE),('Yes','current_residence_or_relocation'))
    def test_requires_confirmed_willingness(self):
        self.assertEqual(candidate.answer('Are you currently living in or ready to relocate to Bengaluru?','CONFIRMED\nCurrent location: Pune, India'),(None,None))
    def test_residence_alone_does_not_get_relocation_yes(self):
        self.assertEqual(candidate.answer('Are you currently living in Bengaluru?',PROFILE),(None,None))
        self.assertEqual(candidate.answer('Are you currently living in Bengaluru and willing to relocate?',PROFILE),(None,None))
    def test_foreign_or_time_constrained_target_stays_unknown(self):
        for target in ['London','Tokyo','Bengaluru within two days']:
            q='Are you currently living in Pune or ready to relocate to '+target+'?'
            self.assertEqual(candidate.answer(q,PROFILE),(None,None))
if __name__=='__main__':unittest.main()
