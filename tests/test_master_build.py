import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import master_build,master_check

class BuildCopyTests(unittest.TestCase):
    def test_source_is_never_modified_and_copy_drops_only_pdftex_specifics(self):
        source=master_build.MASTER_TEX.read_text();copy=master_build.build_copy(source)
        for gone in ('glyphtounicode','pdfgentounicode','lmodern'):self.assertNotIn(gone,copy)
        self.assertIn('Ligatures=NoCommon',copy)
        self.assertIn('glyphtounicode',source)                       # the .tex itself stays pdfTeX-native
        body=lambda t:t.split('\\begin{document}',1)[1]
        self.assertEqual(body(copy),body(source))                    # document content is identical
    def test_a_changed_preamble_fails_loudly_instead_of_building_wrong_fonts(self):
        with self.assertRaises(ValueError):master_build.build_copy('\\documentclass{article}\\begin{document}x\\end{document}')

@unittest.skipUnless(master_build.engine(),'no LaTeX engine available')
class BuildIntegrationTests(unittest.TestCase):
    def test_real_build_is_one_page_ligature_free_and_needs_visual_review_before_install(self):
        tmp=Path(tempfile.mkdtemp());self.addCleanup(shutil.rmtree,tmp,True)
        target=tmp/'Yugal_Teotia_CV.pdf';shutil.copy2(master_build.MASTER_PDF,target)
        before=target.read_bytes()
        with patch.object(master_build,'WORK',tmp/'work'),patch.object(master_build,'MASTER_PDF',target):
            review=master_build.build()
            self.assertTrue(review['automated_ok'],review['checks']);self.assertFalse(review['installed'])
            self.assertTrue(review['checks']['no_ligature_glyphs_in_text'])
            with self.assertRaises(ValueError):master_build.build(install=True)            # no visual attestation yet
        self.assertEqual(target.read_bytes(),before)

class CheckTests(unittest.TestCase):
    def test_master_check_is_green_for_the_installed_pdf(self):
        result=master_check.check();self.assertTrue(result['ok'],result['issues'])

if __name__=='__main__':unittest.main()
