import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from resume_naming import employer_filename

class NamingTests(unittest.TestCase):
    def test_posting_cleanup(self):
        self.assertEqual(employer_filename('Acme - Backend Engineer II - Req 12345 - Final v2', 'Acme', '12345'), 'Yugal_Teotia_Backend_Engineer.pdf')
        self.assertEqual(employer_filename('Java Developer L2'), 'Yugal_Teotia_Java_Developer.pdf')
        self.assertEqual(employer_filename('Full-stack Engineer III'), 'Yugal_Teotia_Full_Stack_Engineer.pdf')
    def test_no_placeholder_or_path(self):
        with self.assertRaises(ValueError): employer_filename('Draft Final AI 123')
        self.assertEqual(employer_filename('../../Backend Engineer'), 'Yugal_Teotia_Backend_Engineer.pdf')

if __name__ == '__main__': unittest.main()
