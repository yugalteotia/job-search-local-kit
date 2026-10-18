"""Person-specific configuration (config/candidate.yaml) and source-drift checks.

Code stays generic; this module is the only reader of the candidate's evidence
catalogue, reusable answers and resume assembly rules.
"""
import re
import datetime as dt
import json
from pathlib import Path

import yaml

from local_paths import ROOT

PATH=ROOT/'config/candidate.yaml'
MASTER=ROOT/'master-resume/Yugal_Teotia_CV.tex'
BASES={'work','profile_work','project','gap'}
TOP={'version','identity','fit','skills','answers','resume'}

def load(path=PATH):
    data=yaml.safe_load(Path(path).read_text())
    if not isinstance(data,dict) or set(data)!=TOP or data['version']!=1:
        raise ValueError('candidate.yaml keys differ from the required schema')
    if data['fit']['fundamentals_policy'] not in ('blocking','non_blocking'):
        raise ValueError('fit.fundamentals_policy must be blocking or non_blocking')
    for name,skill in data['skills'].items():
        if skill['basis'] not in BASES or not skill.get('patterns') or not skill.get('category'):
            raise ValueError('Invalid skill entry: '+name)
        for pattern in skill['patterns']:re.compile(pattern,re.I)
    for pattern in data['fit']['fundamentals']:re.compile(pattern,re.I)
    for rule in data['answers']['rules']:
        re.compile(rule['question'],re.I);re.compile(rule['fact'],re.I)
    return data

def sources(master=None):
    import fit_engine
    profile=(ROOT/'SEARCH_PROFILE.md').read_text()
    return {'master':Path(master or MASTER).read_text(),
            'work':fit_engine.master_work_text(master),
            'projects':fit_engine.master_project_text(master),
            'production_profile':profile.split('### Production skills (done at work)',1)[-1].split('### Project / training skills',1)[0],
            'project_profile':profile.split('### Project / training skills (NOT production)',1)[-1].split('### Not demonstrated',1)[0]}

def drift(data=None,master=None):
    """Return evidence excerpts that no longer exist in the master or profile."""
    import fit_engine
    data=data or load();src=sources(master);missing=[]
    checks={'master_work_excerpt':'work','profile_work_excerpt':'production_profile',
            'master_skill_excerpt':'master','profile_project_excerpt':'project_profile',
            'master_project_excerpt':'projects'}
    for name,skill in data['skills'].items():
        for field,where in checks.items():
            if skill.get(field) and not fit_engine.excerpt_in(src[where],skill[field]):
                missing.append({'skill':name,'field':field,'excerpt':skill[field]})
    profile=(ROOT/'APPLICATION_PROFILE.md').read_text()
    for rule in data['answers']['rules']:
        if not re.search(rule['fact'],profile,re.I):
            missing.append({'answer_rule':rule['id'],'field':'fact','excerpt':rule['fact']})
    return missing

def professional_experience(today=None):
    """Completed employment duration only; month-only dates never gain invented days."""
    today=today or dt.date.today()
    records=json.loads((ROOT/'employment_dates.json').read_text())['employment']
    job=next(x for x in records if x['company']=='IDeaS')
    start=dt.date.fromisoformat(job['start']);end=min(today,dt.date.fromisoformat(job['end']))
    months=(end.year-start.year)*12+end.month-start.month-(end.day<start.day)
    days=(end-start).days
    return {'completed_months':max(0,months),'days':max(0,days),
            'years_decimal':str((max(0,days)*10000//36525)/100),
            'exact_total_known':all('start' in x and 'end' in x for x in records)}

def answer(question,profile,data=None,today=None):
    """Reusable, directly confirmed answer for a normalized question, else (None,None)."""
    data=data or load()
    q=re.sub(r'[^a-z0-9]+',' ',question.lower()).strip()
    if (re.search(r'\b(?:living|residing|based|located)\b',q) and 'relocat' in q
            and re.search(r'\band\b',q) and not re.search(r'\bor\b',q)):
        return None,None  # Relocation willingness alone does not satisfy an AND residence requirement.
    for rule in data['answers']['rules']:
        if re.search(rule['question'],q,re.I) and re.search(rule['fact'],profile,re.I):
            if rule['id']=='current_residence_or_relocation':
                # The OR is satisfied by relocation willingness; do not assert residence.
                # Keep foreign/unknown targets and additional timing requirements unresolved.
                target=re.search(r'\brelocat(?:e|ion)\s+(?:to\s+)?(.+)$',q)
                places=rule['confirmed_india_locations']
                if not target:return None,None
                location=target.group(1)
                if location in ('there','that location','that city'):
                    earlier=q[:target.start()]
                    if not any(re.search(r'\b'+re.escape(city)+r'\b',earlier) for city in places):return None,None
                elif location not in places and not any(location==city+' india' for city in places):return None,None
                return rule['value'],rule['id']
            if rule['id']=='genai_agent_experience':
                import calendar
                from decimal import Decimal,ROUND_DOWN
                y,m=map(int,rule['duration_start_month'].split('-'))
                latest=dt.date(y,m,calendar.monthrange(y,m)[1])
                end=min(today or dt.date.today(),dt.date.fromisoformat(rule['duration_end_date']))
                years=(Decimal(max(0,(end-latest).days))/Decimal('365.25')).quantize(Decimal('0.01'),rounding=ROUND_DOWN)
                return str(years),rule['id']
            if rule['id']=='earliest_joining_date':
                now=today or dt.date.today()
                return ('Immediate' if now>=dt.date(2026,10,1) else rule['value']),rule['id']
            if rule['id']=='backend_experience':
                # A backend duration does not establish tenure in every named
                # technology in a compound screening question.
                other=[name for name,skill in data['skills'].items()
                       if name not in ('java','spring_boot','rest_api','microservices')
                       and any(re.search(p,q,re.I) for p in skill['patterns'])]
                if other:return None,None
                duration=professional_experience(today)
                if re.search(r'how many.*months|in months|number of months',q):return str(duration['completed_months']),rule['id']
                if re.search(r'how many|in years|number of years',q):return duration['years_decimal'],rule['id']
                m=duration['completed_months'];return f'{m//12} year {m%12} months',rule['id']
            if rule['id']=='notice_period':
                now=today or dt.date.today()
                if re.search(r'contractual|original|official duration',q):return None,None
                if now>=dt.date(2026,10,1):
                    if re.search(r'are you.*serving',q):return 'No',rule['id']
                    if re.search(r'\bdays\b',q):return '0',rule['id']
            if rule['id']=='last_annual_ctc':
                if re.search(r'expected|desired|monthly|in hand|take home',q):return None,None
                line=next((x for x in profile.splitlines() if x.startswith('- Current CTC:')),'')
                pattern=r'₹([0-9.]+) LPA fixed' if re.search(r'fixed|base',q) else r'₹([0-9.]+) LPA performance.based variable' if 'variable' in q else r'Current CTC: ₹([0-9.]+) LPA'
                match=re.search(pattern,line)
                if not match:return None,None
                from decimal import Decimal
                if re.search(r'rupees|\binr\b',q):return str(int(Decimal(match.group(1))*100000)),rule['id']
                return match.group(1)+' LPA '+('fixed' if re.search(r'fixed|base',q) else 'variable' if 'variable' in q else 'annual total CTC'),rule['id']
            if rule['id']=='total_experience' and re.search(r'exact|how many|in years|number of years',q):
                return None,None  # Data Nova has only confirmed month/year dates.
            return rule['value'],rule['id']
    return None,None
