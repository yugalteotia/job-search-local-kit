"""Focused checks for transferable UI evidence and explicit experience gates."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import auto_fit,candidate,fit_engine,job_config,review_candidates

JD='''Location: Pune, Bengaluru, Chennai
Experience: 2 - 5 years
Job description
Role Summary
We seek strong expertise in backend development using Java and Spring Boot, along with modern frontend technologies such as Angular or React. Build enterprise applications.
Key Responsibilities
Design and build RESTful APIs and Microservices.
Develop responsive and user-friendly UI components.
Required Skills
Java and Spring Boot
Angular (preferred) or React
TypeScript
'''

class FitReassessment(unittest.TestCase):
    def verdict(self,jd=JD):
        row={'jd_text':jd,'title':'Java Full Stack Developer','location':'Pune','availability':'Live','checked_at':'2026-10-13T16:06:35+00:00'}
        cfg=job_config.load();cfg['high_fit']['secondary_skills_policy']='block'
        p,reasons,_=auto_fit.build_packet(row,candidate.load(),cfg)
        if reasons:return None,reasons
        return fit_engine.evaluate(row,p,cfg),p

    def test_backend_depth_does_not_invent_production_react(self):
        v,p=self.verdict()
        self.assertTrue(v['strong_medium_eligible']);self.assertFalse(v['eligible'])
        self.assertEqual(v['fit'],'Medium');self.assertGreater(v['essential_transferable_supported'],0)
        self.assertLess(v['essential_supported'],v['essential_total'])
        self.assertTrue(all(x['basis']=='project' for x in p['requirements'] if x['name']=='react'))

    def test_explicit_production_react_is_not_transferred(self):
        v,_=self.verdict(JD+'Mandatory production React expertise.\n')
        self.assertFalse(v['strong_medium_eligible']);self.assertFalse(v['eligible'])

    def test_unrelated_essential_gap_still_blocks(self):
        v,_=self.verdict(JD+'Required Skills\nMust have hands-on Kafka experience.\n')
        self.assertFalse(v['strong_medium_eligible']);self.assertFalse(v['eligible'])

    def test_word_range_in_intro_is_a_hard_minimum(self):
        jd='''Location: India
About the job
Work with clients and deliver projects. You need at least five to seven years of software engineering experience.
Requirements:
Hands-on programming experience using relevant languages.
'''
        cfg=job_config.load();row={'jd_text':jd,'title':'Software Engineer','location':'India','checked_at':'2026-10-13T16:06:35+00:00'}
        reviewed=review_candidates.assess(row,cfg)
        self.assertEqual(reviewed['experience_minimum'],5)
        self.assertIn('five to seven years',reviewed['evidence']['experience'])
        p,reasons,_=auto_fit.build_packet(row,candidate.load(),cfg)
        self.assertIsNone(p);self.assertEqual(reasons,['Required experience starts at 5 years'])

    def test_numeric_ranges_continue_to_parse(self):
        for text in ('2 - 5 years','2 to 5 years','two to five years'):
            low,excerpt,_,errors=auto_fit._experience('Experience: '+text,'',candidate.load())
            self.assertFalse(errors);self.assertEqual(low,2);self.assertIn(text,excerpt)

if __name__=='__main__':unittest.main()
