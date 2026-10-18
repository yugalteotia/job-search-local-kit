import sys
import unittest
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'tools')]
import auto_fit,candidate,job_config

class Alternatives(unittest.TestCase):
    def test_explicit_frontend_alternative_survives_other_required_skills(self):
        jd='''Location: Pune
Experience: 2 - 3 years
Job description
Required Skills
2-3 years of Java development experience.
Strong hands-on experience in Java, Spring Boot and Microservices.
Build RESTful APIs for backend systems.
Experience with Angular/React, HTML5, CSS3, JavaScript, and TypeScript.
'''
        packet,reasons,notes=auto_fit.build_packet({'title':'Java Full Stack Developer','jd_text':jd},candidate.load(),job_config.load())
        self.assertEqual(reasons,[])
        names={x['name'] for x in packet['requirements']}
        self.assertNotIn('other_frontend',names)
        self.assertIn('react',names);self.assertIn('ember_typescript',names)
        self.assertTrue(any(x['basis']=='project' for x in packet['requirements']))

if __name__=='__main__':unittest.main()
