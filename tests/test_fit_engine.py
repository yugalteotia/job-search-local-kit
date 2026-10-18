import hashlib
import sys
from pathlib import Path
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import fit_engine,job_config

class FitTests(unittest.TestCase):
    def setUp(self):
        self.config=job_config.load()
        self.jd='Java engineering. Spring Boot services. Build backend REST APIs. Bengaluru, India. 1-3 years of professional experience. Essential: REST and gRPC APIs.'
        self.job={'jd_text':self.jd,'availability':'Live','location':'Bengaluru, India','checked_at':'2026-09-28T00:00:00+00:00'}
        self.decision={'jd_sha256':hashlib.sha256(self.jd.encode()).hexdigest(),
             'java_excerpt':'Java engineering','spring_boot_excerpt':'Spring Boot services',
             'backend_excerpt':'Build backend REST APIs','location_excerpt':'Bengaluru, India',
             'experience_excerpt':'1-3 years of professional experience','required_professional_years':1,
             'requirements':[{'name':'REST APIs','essential':True,'jd_excerpt':'Essential: REST and gRPC APIs',
                              'basis':'work','master_work_excerpt':'Designed and implemented REST and gRPC APIs'}]}
    def test_exact_jd_and_work_can_pass(self):
        verdict=fit_engine.evaluate(self.job,self.decision,self.config)
        self.assertEqual(verdict['fit'],'High')
        self.assertTrue(verdict['eligible'])
    def test_forged_excerpt_or_project_only_is_blocked(self):
        self.decision['requirements'][0]['jd_excerpt']='Essential: Kubernetes and Kafka'
        self.assertFalse(fit_engine.evaluate(self.job,self.decision,self.config)['eligible'])
        self.decision['requirements'][0]['jd_excerpt']='Essential: REST and gRPC APIs'
        self.decision['requirements'][0]['basis']='project'
        self.assertFalse(fit_engine.evaluate(self.job,self.decision,self.config)['eligible'])
    def test_demonstrated_project_skill_can_support_ordinary_required_skill(self):
        self.job['jd_text']+=' Essential: Spring Data JPA and MySQL persistence.'
        self.decision['jd_sha256']=hashlib.sha256(self.job['jd_text'].encode()).hexdigest()
        self.decision['requirements'].append({
            'name':'Spring Data JPA and MySQL','essential':True,
            'jd_excerpt':'Essential: Spring Data JPA and MySQL persistence',
            'basis':'project',
            'master_project_excerpt':'Spring Boot, Spring Data JPA, MySQL, Flyway, OpenAPI'})
        verdict=fit_engine.evaluate(self.job,self.decision,self.config)
        self.assertTrue(verdict['eligible'],verdict['reasons'])
        self.assertEqual(verdict['essential_project_supported'],1)
    def test_project_does_not_claim_professional_depth(self):
        self.job['jd_text']+=' Essential: 3 years of production Spring Data JPA experience.'
        self.decision['jd_sha256']=hashlib.sha256(self.job['jd_text'].encode()).hexdigest()
        self.decision['requirements'].append({
            'name':'production Spring Data JPA','essential':True,
            'jd_excerpt':'3 years of production Spring Data JPA experience',
            'basis':'project',
            'master_project_excerpt':'Spring Boot, Spring Data JPA, MySQL, Flyway, OpenAPI'})
        verdict=fit_engine.evaluate(self.job,self.decision,self.config)
        self.assertFalse(verdict['eligible'])
        self.assertIn('professional or advanced depth',str(verdict['reasons']))
    def test_confirmed_profile_work_stays_separate_from_project_section(self):
        self.job['jd_text']+=' Essential: Spring MVC backend services.'
        self.decision['jd_sha256']=hashlib.sha256(self.job['jd_text'].encode()).hexdigest()
        item={'name':'Spring MVC','essential':True,'jd_excerpt':'Essential: Spring MVC backend services',
              'basis':'profile_work',
              'profile_work_excerpt':'Java (Java 17 at IDeaS), Spring Boot and Spring MVC',
              'master_work_excerpt':'20 Java modules from Spring Boot 2.6 to Spring Boot 3.5'}
        self.decision['requirements'].append(item)
        self.assertTrue(fit_engine.evaluate(self.job,self.decision,self.config)['eligible'])
        item['profile_work_excerpt']='the project uses Spring MVC and Java 8+ language features'
        self.assertFalse(fit_engine.evaluate(self.job,self.decision,self.config)['eligible'])
    def test_experience_and_changed_jd_blocked(self):
        self.decision['required_professional_years']=3
        self.assertFalse(fit_engine.evaluate(self.job,self.decision,self.config)['eligible'])
        self.decision['required_professional_years']=1
        self.job['location']='New York, NY'
        self.assertFalse(fit_engine.evaluate(self.job,self.decision,self.config)['eligible'])
        self.job['location']='Bengaluru, India'
        self.job['jd_text']+=' New requirement: Kafka.'
        self.assertFalse(fit_engine.evaluate(self.job,self.decision,self.config)['eligible'])
    def test_only_evidence_complete_three_year_role_is_strong_medium(self):
        self.job['jd_text']=self.jd.replace('1-3 years','3-3 years')
        self.decision['jd_sha256']=hashlib.sha256(self.job['jd_text'].encode()).hexdigest()
        self.decision['experience_excerpt']='3 years of professional experience'
        self.decision['required_professional_years']=3
        verdict=fit_engine.evaluate(self.job,self.decision,self.config)
        self.assertEqual(verdict['fit'],'Medium')
        self.assertTrue(verdict['strong_medium_eligible'],verdict['reasons'])
        self.decision['experience_excerpt']='4 years of professional experience'
        self.decision['required_professional_years']=4
        self.assertFalse(fit_engine.evaluate(self.job,self.decision,self.config)['strong_medium_eligible'])
    def test_essential_cloud_or_kafka_is_not_strong_medium(self):
        self.job['jd_text']=self.jd.replace('1-3 years','3 years')+' Essential: Kafka experience.'
        self.decision['jd_sha256']=hashlib.sha256(self.job['jd_text'].encode()).hexdigest()
        self.decision['experience_excerpt']='3 years of professional experience'
        self.decision['required_professional_years']=3
        self.decision['requirements'][0]={'name':'Kafka','essential':True,
            'jd_excerpt':'Essential: Kafka experience','basis':'gap'}
        self.assertFalse(fit_engine.evaluate(self.job,self.decision,self.config)['strong_medium_eligible'])

if __name__=='__main__':unittest.main()
