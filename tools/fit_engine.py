"""Validate AI-extracted JD requirements against exact original and master excerpts."""
import hashlib
import json
import re
from pathlib import Path

from local_paths import ROOT
from experience_ranges import YEARS,number as experience_number

SPRING_FRAMEWORK=re.compile(r'spring\s*(?:boot|mvc|framework|core)|\bspring\b(?!\s*(?:cloud|batch|data|security|integration|webflux))',re.I)

def flat(text):return re.sub(r'\s+',' ',str(text or '')).strip().casefold()
def excerpt_in(source,excerpt):return len(flat(excerpt))>=12 and flat(excerpt) in flat(source)
def master_work_text(master_path=None):
    source=Path(master_path or ROOT/'master-resume/Yugal_Teotia_CV.tex').read_text()
    blocks=re.findall(r'\\begin\{itemize\}(.*?)\\end\{itemize\}',source,re.S)
    # The first two bullet blocks are IDeaS and Data Nova work, per generic master.
    work=' '.join(blocks[:2])
    work=re.sub(r'\\(?:textbf|textit)\{([^{}]*)\}',r'\1',work)
    return work.replace('\\&','&').replace('\\%','%').replace('~',' ')

def master_project_text(master_path=None):
    source=Path(master_path or ROOT/'master-resume/Yugal_Teotia_CV.tex').read_text()
    section=source.split(r'\section{PROJECTS}',1)[-1].split(r'\section{EDUCATION}',1)[0]
    section=re.sub(r'\\(?:textbf|textit|href)\{([^{}]*)\}',r'\1',section)
    return section.replace('\\&','&').replace('\\%','%').replace('~',' ')

def _nonwork_support(item,master,projects,profile):
    basis=item.get('basis')
    excerpt=item.get('jd_excerpt') or ''
    if basis=='gap':
        return False,'No confirmed evidence for '+str(item.get('name'))
    # A project can establish practical familiarity, but never a JD's
    # expressly professional, production, advanced or multi-year depth.
    if re.search(r'\b(?:production|professional|expertise|expert|advanced|deep|enterprise(?:.scale)?)\b|\b\d+(?:\.\d+)?\s*\+?\s*(?:years?|yrs?)\b',excerpt,re.I):
        return False,'JD requires professional or advanced depth for '+str(item.get('name'))
    if basis=='project':
        direct=excerpt_in(projects,item.get('master_project_excerpt'))
        corroborated=(excerpt_in(profile,item.get('profile_project_excerpt'))
                      and (excerpt_in(master,item.get('master_skill_excerpt'))
                           or excerpt_in(projects,item.get('master_project_excerpt'))))
        return (True,None) if direct or corroborated else (False,'Project evidence is missing for '+str(item.get('name')))
    if basis=='training':
        if re.search(r'\b(?:hands.on|practical|experience|built|implemented)\b',excerpt,re.I):
            return False,'Training alone does not establish practical depth for '+str(item.get('name'))
        corroborated=(excerpt_in(profile,item.get('profile_training_excerpt'))
                      and excerpt_in(master,item.get('master_skill_excerpt')))
        return (True,None) if corroborated else (False,'Training evidence is missing for '+str(item.get('name')))
    return False,'Unsupported evidence basis for '+str(item.get('name'))

def transferable_frontend(item,jd,work,master,projects,profile):
    """Transfer UI delivery, never claim production experience in another framework."""
    if item.get('name')!='react' or item.get('basis')!='project':return False
    excerpt=item.get('transferable_frontend_work_excerpt')
    if not excerpt_in(work,excerpt) or not re.search(r'ember',excerpt or '',re.I) or not re.search(r'typescript',excerpt or '',re.I):return False
    if not (excerpt_in(profile,item.get('profile_project_excerpt'))
            and excerpt_in(master,item.get('master_skill_excerpt'))):return False
    # Check depth in the framework's clause, not backend expertise elsewhere
    # in the same role-summary sentence.
    for clause in re.split(r'\n|[.;]|,\s*|\balong with\b',jd,flags=re.I):
        if not re.search(r'\breact\b|\bangular\b',clause,re.I):continue
        if re.search(r'\b(?:must|mandatory|required|professional|production|advanced|expert\w*|strong|deep|hands.on|proficien\w*)\b|\b'+YEARS.pattern,clause,re.I):return False
    return True

def evaluate(job,decision,config,master_path=None):
    """Return High only when every essential JD item and work basis is exact."""
    jd=job.get('jd_text') or ''
    simple=config['high_fit'].get('secondary_skills_policy')=='interview_preparation'
    result={'fit':'Medium','eligible':False,'reasons':[],'jd_sha256':hashlib.sha256(jd.encode()).hexdigest(),
            'essential_total':0,'essential_work_supported':0,
            'essential_project_supported':0,'essential_training_supported':0,'essential_supported':0,
            'essential_transferable_supported':0,'transferable_gaps':[],
            'strong_medium_eligible':False,'interview_preparation_gaps':[]}
    if decision.get('jd_sha256')!=result['jd_sha256']:
        result['reasons'].append('JD hash changed');return result
    if job.get('availability')!='Live' or not job.get('checked_at'):
        result['reasons'].append('Original listing not verified live');return result
    fields=('java_excerpt','backend_excerpt','location_excerpt','experience_excerpt')
    for field in fields:
        excerpt=decision.get(field)
        if not excerpt_in(jd,excerpt):result['reasons'].append('Missing exact original JD excerpt: '+field)
    if result['reasons']:return result
    spring_excerpt=decision.get('spring_boot_excerpt')
    spring_stated=bool(spring_excerpt and excerpt_in(jd,spring_excerpt) and SPRING_FRAMEWORK.search(spring_excerpt))
    if spring_excerpt and not spring_stated:
        result['reasons'].append('Spring framework excerpt is not exact supported JD evidence')
    result['framework_status']='spring_stated' if spring_stated else 'not_stated'
    if simple and not spring_stated:result['reasons'].append('Spring/Spring Boot backend development is not established')
    if not re.search(r'\bjava\b',decision['java_excerpt'],re.I):
        result['reasons'].append('Java not explicit in the cited JD excerpt')
    if not re.search(r'backend|back.end|rest|\bapi\b|microservice',decision['backend_excerpt'],re.I):
        result['reasons'].append('Substantial backend work not established by cited excerpt')
    if not re.search(r'\bindia\b|pune|bengaluru|bangalore|hyderabad|delhi|gurugram|noida|mumbai|chennai',decision['location_excerpt'],re.I):
        result['reasons'].append('India location not established')
    location=str(job.get('location') or '')
    if not re.search(r'\bindia\b|pune|bengaluru|bangalore|hyderabad|delhi|gurugram|noida|mumbai|chennai',location,re.I):
        result['reasons'].append('Normalized posting location is not established in India')
    minimum=decision.get('required_professional_years')
    if simple and isinstance(minimum,(int,float)) and minimum>config['high_fit']['maximum_approved_near_miss_years']:
        result['reasons'].append('Required experience exceeds the approved 3-year near-miss band')
    if type(minimum) not in (int,float) or minimum<0 or minimum>config['high_fit']['maximum_required_professional_years']:
        result['reasons'].append('Experience minimum missing or beyond confirmed profile band')
    years=YEARS.search(decision['experience_excerpt'])
    if not years:
        result['reasons'].append('Experience band is not explicit in cited JD excerpt')
    elif experience_number(years.group(1))!=float(minimum if minimum is not None else -1):
        result['reasons'].append('Experience minimum does not match cited JD')
    items=decision.get('requirements')
    if not isinstance(items,list) or not items:
        result['reasons'].append('No itemized original JD requirements');return result
    master=Path(master_path or ROOT/'master-resume/Yugal_Teotia_CV.tex').read_text()
    work=master_work_text(master_path);projects=master_project_text(master_path)
    profile=(ROOT/'SEARCH_PROFILE.md').read_text()
    production_profile=profile.split('### Production skills (done at work)',1)[-1].split('### Project / training skills',1)[0]
    project_profile=profile.split('### Project / training skills (NOT production)',1)[-1].split('### Not demonstrated',1)[0]
    core_work=False
    def gap(item,reason):
        if simple and item.get('name') not in ('java','spring_boot','rest_api','microservices'):
            result['interview_preparation_gaps'].append({'name':item.get('name'),'reason':reason,'jd_excerpt':item.get('jd_excerpt'),'confirmed_basis':item.get('basis')})
        else:result['reasons'].append(reason)
    for item in items:
        if not isinstance(item,dict) or not item.get('essential'):continue
        result['essential_total']+=1
        if not excerpt_in(jd,item.get('jd_excerpt')):
            result['reasons'].append('Essential requirement lacks exact JD excerpt: '+str(item.get('name')));continue
        basis=item.get('basis')
        if basis in ('work','profile_work'):
            supported=(excerpt_in(work,item.get('master_work_excerpt')) if basis=='work' else
                       excerpt_in(production_profile,item.get('profile_work_excerpt'))
                       and (excerpt_in(master,item.get('master_skill_excerpt'))
                            or excerpt_in(work,item.get('master_work_excerpt'))))
            if not supported:
                gap(item,'Essential requirement lacks confirmed production evidence: '+str(item.get('name')));continue
            result['essential_work_supported']+=1
            if re.search(r'\bjava\b|spring\s*boot|backend|back.end|\brest\b|\bapi\b',item['jd_excerpt'],re.I):core_work=True
        else:
            if transferable_frontend(item,jd,work,master,projects,project_profile):
                result['essential_transferable_supported']+=1
                result['transferable_gaps'].append({'name':item['name'],'gap':'React is project-level; Angular is unconfirmed. Professional Ember.js/TypeScript UI work is transferable.'})
                gap(item,'Frontend framework gap has confirmed transferable professional UI evidence')
                continue
            supported,reason=_nonwork_support(item,master,projects,project_profile)
            if not supported:gap(item,reason);continue
            result['essential_project_supported' if basis=='project' else 'essential_training_supported']+=1
        result['essential_supported']+=1
    if result['essential_total']==0:result['reasons'].append('No essential requirements enumerated')
    if not core_work:result['reasons'].append('Core Java/backend duty lacks production-work evidence')
    coverage=result['essential_supported']/max(1,result['essential_total'])
    if simple:
        for name in decision.get('notes',{}).get('other_gaps',[]):
            if not any(g['name']==name for g in result['interview_preparation_gaps']):result['interview_preparation_gaps'].append({'name':name,'reason':'Named secondary technology has no confirmed matching evidence'})
        only_near_miss='Experience minimum missing or beyond confirmed profile band'
        if (core_work and spring_stated and isinstance(minimum,(int,float))
                and 0<=minimum<=config['high_fit']['maximum_approved_near_miss_years']
                and all(reason==only_near_miss for reason in result['reasons'])):
            if result['interview_preparation_gaps'] or result['reasons'] or coverage<1:
                result['fit']='Medium';result['strong_medium_eligible']=True
            else:result['fit']='High';result['eligible']=True
        elif coverage<.5:result['fit']='Low'
        return result
    if coverage<config['high_fit']['minimum_essential_coverage']:
        result['reasons'].append('Essential evidence coverage below 100%')
    if not result['reasons'] and spring_stated:
        result['fit']='High';result['eligible']=True
    elif coverage<.5:result['fit']='Low'
    # Strong Medium can retain a named frontend framework gap when verified
    # professional UI delivery transfers and the JD has no explicit depth gate.
    # Unrelated essential gaps still block, regardless of coverage percentage.
    # Framework-neutral Java backend roles can be strong Medium; absence of
    # Spring wording is not a skill gap. Experience near-misses remain bounded.
    # A 4+ year minimum or essential Kafka/cloud/infrastructure expertise is
    # always excluded.
    allowed_medium_reasons={
        'Experience minimum missing or beyond confirmed profile band',
        'Frontend framework gap has confirmed transferable professional UI evidence',
        'Essential evidence coverage below 100%',
        f'Required experience starts at {minimum:g} years, above the {config["high_fit"]["maximum_required_professional_years"]:g}-year High-fit gate'
        if isinstance(minimum,(int,float)) else '',
    }
    essential_text=' '.join(str(item.get('name',''))+' '+str(item.get('jd_excerpt',''))
                            for item in items if isinstance(item,dict) and item.get('essential'))
    hard_medium_gap=bool(re.search(r'kafka|cloud|\baws\b|\bgcp\b|\bazure\b|kubernetes|infrastructure',essential_text,re.I))
    if (result['fit']=='Medium' and isinstance(minimum,(int,float))
            and 0<=minimum<config['naukri_worker']['medium_max_required_years_exclusive']
            and (not spring_stated or minimum>config['high_fit']['maximum_required_professional_years'] or result['essential_transferable_supported']>0)
            and result['essential_total']>0
            and result['essential_supported']+result['essential_transferable_supported']==result['essential_total']
            and coverage>=.75
            and core_work and not hard_medium_gap
            and all(reason in allowed_medium_reasons for reason in result['reasons'])):
        result['strong_medium_eligible']=True
    return result
