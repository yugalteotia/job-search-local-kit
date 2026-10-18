"""Deterministic Naukri fit review: exact JD excerpts + candidate evidence -> fit packet.

Code only. Person-specific judgement lives in config/candidate.yaml. The packet
is validated by fit_engine.evaluate (exact JD, master and profile excerpts) and
nothing here relaxes that gate. Anything uncertain stays unqueued with reasons.
"""
import datetime as dt
import hashlib
import json
import re

import candidate
import fit_engine
import job_db
import review_candidates
from experience_ranges import YEARS,number as experience_number

DDL='''CREATE TABLE IF NOT EXISTS auto_fit_log(
 job_id INTEGER NOT NULL,jd_sha256 TEXT NOT NULL,at TEXT NOT NULL,outcome TEXT NOT NULL,
 fit TEXT,reasons_json TEXT NOT NULL,PRIMARY KEY(job_id,jd_sha256))'''
CONTEXT_DDL='''CREATE TABLE IF NOT EXISTS auto_fit_context(
 job_id INTEGER NOT NULL,jd_sha256 TEXT NOT NULL,context_sha256 TEXT NOT NULL,
 PRIMARY KEY(job_id,jd_sha256))'''

def context_fingerprint(config,cand,row):
    """Cache the decision inputs, not merely the JD. Never store candidate values."""
    root=candidate.ROOT
    files=('APPLICATION_PROFILE.md','master-resume/Yugal_Teotia_CV.tex',
           'tools/auto_fit.py','tools/fit_engine.py','tools/review_candidates.py')
    payload={'candidate':cand,'high_fit':config['high_fit'],'naukri_policy':config['naukri_worker'],
             'experience':candidate.professional_experience(),
             'files':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files},
             'title':row['title'],'location':row['location'],'provenance':json.loads(row['provenance_json'] or '{}').get('duplicate_check')}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,default=str).encode()).hexdigest()
OPTIONAL=re.compile(r'good to have|nice to have|preferred|desirable|added advantage|bonus|a plus\b|plus point|would be an advantage|exposure to|familiarity with|awareness of|basic knowledge|basic understanding|working knowledge',re.I)
HEAD_OPTIONAL=re.compile(r'preferred|good to have|nice to have|desirable|bonus|optional|\bplus\b|advantage',re.I)
HEAD_REQUIRED=re.compile(r'mandatory|required|requirements?|must[- ]have|essential|qualifications?|technical skills|skills required|key skills|what we.re looking for|you have',re.I)
HEAD_DUTY=re.compile(r'responsibilit|duties|what you|role overview|job summary|overview|about the|roles? and|description',re.I)
HEAD_IGNORE=re.compile(r'soft skills|benefits|perks|about us|why join|what we offer|education',re.I)
DEPTH=re.compile(r'\b(?:strong|hands.on|expert\w*|proficien\w*|extensive|deep|solid|must|mandatory|required)\b',re.I)
NOT_EXPERIENCE=re.compile(r'notice|\bdays\b|founded|established|since|clients|countries|employees|salary|\bctc\b',re.I)
INDIA=re.compile(r'\bindia\b|pune|bengaluru|bangalore|hyderabad|delhi|gurugram|gurgaon|noida|mumbai|chennai',re.I)

def _split(jd):
    match=re.search(r'^Job description\s*$',jd,re.M|re.I)
    head=jd[:match.start()] if match else ''
    body=jd[match.end():] if match else jd
    cut=re.search(r'^(?:Role\s*:|Industry Type\s*:|Key Skills\s*$|Report this job|read more)',body,re.M)
    return head,(body[:cut.start()] if cut else body)

def _heading(line):
    if len(line)>60 or line.endswith('.') or len(line.split())>7:return None
    if HEAD_IGNORE.search(line):return 'ignore'
    if HEAD_OPTIONAL.search(line):return 'optional'
    if HEAD_REQUIRED.search(line):return 'required'
    if HEAD_DUTY.search(line):return 'duty'
    return None

def _window(line,start,end,width,previous=''):
    left=max(0,start-width);right=min(len(line),end+width)
    while left>0 and left<start and line[left-1].isalnum() and line[left].isalnum():left+=1
    while right<len(line) and right>end and line[right-1].isalnum() and line[right].isalnum():right-=1
    text=line[left:right].strip()
    while len(text)<12 and (left>0 or right<len(line)):
        left=max(0,left-10);right=min(len(line),right+10);text=line[left:right].strip()
    if len(text)<12 and previous:text=previous+' '+text
    return text

def _lines(body):
    ctx='duty';previous=''
    for raw in body.split('\n'):
        line=raw.strip()
        if not line:continue
        head=_heading(line)
        if head:ctx=head;previous=line;continue
        # Learning a tool is a duty, not a demand for prior production expertise.
        # Scope optional secondary-language exposure to its clause.
        parts=re.split(r'(?=with exposure to)',line,flags=re.I)
        for part in parts:
            optional=bool(OPTIONAL.search(part)) or bool(re.match(r'(?:Learn\b|Eager to learn\b)',part,re.I))
            yield part,('optional' if optional else ctx),previous
        previous=line

def _experience(head,body,cand,secondary_as_gaps=False):
    mins=[];upper=None;excerpts={}
    for source in (head,body):
        for line in source.split('\n'):
            line=line.strip()
            if not line or not re.search(r'experience|\bexp\b|years|yrs',line,re.I):continue
            for m in YEARS.finditer(line):
                start=line.rfind('.',0,m.start())+1
                end=line.find('.',m.end())
                clause=line[start:end if end>=0 else len(line)]
                if NOT_EXPERIENCE.search(clause):continue
                if secondary_as_gaps:
                    named={name for name,skill in cand['skills'].items() if any(re.search(p,clause,re.I) for p in skill['patterns'])}
                    core=named.intersection({'java','spring_boot','rest_api','microservices'})
                    overall=re.search(r'overall|total|backend|software (?:engineering|development)',clause,re.I)
                    if named and not core and not overall:continue
                low=experience_number(m.group(1));high=m.group(2)
                mins.append(low);excerpts.setdefault(low,(line,m))
                if high and source is head:upper=experience_number(high)
    if not mins:return None,None,None,['Experience band is not stated in the JD']
    low=max(mins);line,m=excerpts[low]
    excerpt=line if YEARS.search(line) and experience_number(YEARS.search(line).group(1))==low else line[m.start():m.end()]
    engine=YEARS.search(excerpt)
    if not engine or experience_number(engine.group(1))!=low:
        return None,None,None,['Experience minimum cannot be cited unambiguously']
    reasons=[]
    if upper is not None and upper<cand['fit']['min_band_upper_years']:reasons.append('Experience band is below the candidate seniority')
    return low,excerpt,reasons,[]

def _first(pattern,lines,width=60):
    rx=re.compile(pattern,re.I)
    for line,ctx,previous in lines:
        m=rx.search(line)
        if m and ctx!='optional':return _window(line,m.start(),m.end(),width,previous)
    return None

def build_packet(row,cand,config):
    """Return (packet or None, reasons, notes)."""
    jd=row['jd_text'] or '';head,body=_split(jd);lines=list(_lines(body))
    reasons=[];notes={'other_gaps':[],'unverified_fundamentals':[]}
    title=row['title'] or ''
    for term in cand['fit']['exclude_title_terms']:
        if re.search(r'\b'+re.escape(term)+r'\b',title,re.I):
            return None,['Title indicates a different seniority band: '+term],notes
    simple=config['high_fit'].get('secondary_skills_policy')=='interview_preparation'
    low,exp_excerpt,band,exp_reasons=_experience(head,body,cand,secondary_as_gaps=simple)
    if exp_reasons:return None,exp_reasons,notes
    reasons+=band
    if low>=config['naukri_worker']['medium_max_required_years_exclusive']:
        return None,[f'Required experience starts at {low:g} years'],notes
    if simple and low>config['high_fit']['maximum_approved_near_miss_years']:
        return None,[f'Required experience starts at {low:g} years; approved near-misses stop at 3 years'],notes
    if simple and re.search(r'(?:primarily|mainly|predominantly|primary stack|core stack|focused on)\s*:?\s+(?:\w+\s+){0,4}(?:python|django|\.net|c#|node\.?js|react native|flutter|rust)\b',body,re.I):
        return None,['Primary stack is outside Java/Spring backend development'],notes
    location=_first(r'^Location:.*$',[(l.strip(),'x','') for l in head.split('\n')],0) or _first(INDIA.pattern,lines,30)
    # Optional exposure to a second language must not hide Java in the same sentence.
    java=_first(r'(?<![A-Za-z])java(?![A-Za-z])',lines if simple else [(l,'evidence',p) for l,c,p in lines])
    spring_pattern=fit_engine.SPRING_FRAMEWORK.pattern
    spring=_first(spring_pattern,lines)
    if not simple:spring=spring or _first(spring_pattern,[(line,'required',previous) for line,ctx,previous in lines])
    backend_lines=[(l,'evidence',p) for l,c,p in lines]
    backend=_first(r'backend|back.end',backend_lines) or _first(r'micro.?services?|\brest\w*\b|\bapis?\b',backend_lines)
    for label,value in (('location',location),('Java',java),('backend work',backend)):
        if not value:reasons.append(label+' is not explicit in the JD body')
    if simple and not spring:reasons.append('Spring/Spring Boot backend development is not established')
    if reasons:return None,reasons,notes
    fundamentals=[re.compile(x,re.I) for x in cand['fit']['fundamentals']]
    any_of=set(cand['fit']['any_of_categories'])
    found={}
    for line,ctx,previous in lines:
        if ctx=='ignore':continue
        for rx in fundamentals:
            m=rx.search(line)
            if m:
                label=m.group(0).lower()
                if ctx in ('required','duty') and label not in notes['unverified_fundamentals']:notes['unverified_fundamentals'].append(label)
        hits={}
        for name,skill in cand['skills'].items():
            for pattern in skill['patterns']:
                m=re.search(pattern,line,re.I)
                if m:hits[name]=m;break
        if not hits:continue
        alternatives=' or ' in line.lower() or bool(re.search(r'/',line))
        for name,m in hits.items():
            skill=cand['skills'][name];essential=ctx in ('required','duty')
            if name=='iac_devops' and re.search(r'devops\s+culture',line,re.I) and not re.search(r'terraform|ansible|infrastructure.as.code',line,re.I):
                essential=False
            stack_line=any(n in hits for n in cand['fit']['core_skills'])
            if (skill['basis']=='gap' and ctx=='duty' and not DEPTH.search(line) and not stack_line
                    and name not in ('ai_agentic','mobile_delivery','nextjs','other_frameworks')):essential=False
            if not essential:
                if skill['basis']=='gap' and name not in notes['other_gaps']:notes['other_gaps'].append(name)
                continue
            group=None
            if skill['category'] in any_of or alternatives:
                same=[n for n in hits if cand['skills'][n]['category']==skill['category']]
                clause_start=line.rfind(';',0,m.start())+1
                clause_end=line.find(';',m.end())
                clause=line[clause_start:clause_end if clause_end>=0 else len(line)]
                clause_same=[n for n in same if clause_start<=hits[n].start()<(clause_end if clause_end>=0 else len(line))]
                ordered=sorted(same,key=lambda n:hits[n].start())
                edges=[(a,b) for a,b in zip(ordered,ordered[1:]) if re.fullmatch(
                    r'\s*(?:/|or)\s*',line[hits[a].end():hits[b].start()],re.I)]
                component={name}
                for _ in ordered:
                    for a,b in edges:
                        if a in component or b in component:component.update((a,b))
                # An unrelated "and TypeScript" later in a sentence does not
                # turn the explicit Angular/React alternative into two requirements.
                if len(component)>1:group=(skill['category'],line,tuple(sorted(component)))
                elif len(same)>1 and not re.search(r'\band\b|&',line,re.I) and re.search(r'\bor\b|such as|e\.g\.',line,re.I):
                    group=(skill['category'],line,tuple(sorted(same)))
                elif len(clause_same)>1 and not re.search(r'\band\b|&',clause,re.I) and re.search(r'\bor\b',clause,re.I):
                    group=(skill['category'],line,tuple(sorted(clause_same)))
            wide=skill['basis'] in ('project','gap')
            excerpt=_window(line,0,len(line),0,previous) if wide and len(line)<=240 else _window(line,m.start(),m.end(),110 if wide else 30,previous)
            entry={'name':name,'essential':True,'jd_excerpt':excerpt,'basis':skill['basis'],'_group':group}
            for field in ('master_work_excerpt','profile_work_excerpt','master_skill_excerpt','profile_project_excerpt','master_project_excerpt'):
                if skill.get(field):entry[field]=skill[field]
            if name=='react' and skill['basis']=='project':
                entry['transferable_frontend_work_excerpt']=cand['skills']['ember_typescript']['master_work_excerpt']
            found.setdefault((name,group,line),entry)
    rank={'work':0,'profile_work':1,'project':2,'gap':3}
    items=[];groups={}
    for entry in found.values():
        if entry['_group']:groups.setdefault(entry['_group'],[]).append(entry)
        else:items.append(entry)
    for members in groups.values():
        items.append(min(members,key=lambda e:rank[e['basis']]))
    seen=set();unique=[]
    for item in items:
        key=(item['name'],item['jd_excerpt'])
        if key in seen:continue
        seen.add(key);item.pop('_group',None);unique.append(item)
    if cand['fit']['fundamentals_policy']=='blocking':
        for label in notes['unverified_fundamentals']:
            unique.append({'name':'fundamental:'+label,'essential':True,'jd_excerpt':label,'basis':'gap'})
    if not unique:return None,['No essential requirements could be itemized'],notes
    packet={'jd_sha256':hashlib.sha256(jd.encode()).hexdigest(),'java_excerpt':java,'spring_boot_excerpt':spring,
            'framework_status':'spring_stated' if spring else 'not_stated',
            'backend_excerpt':backend,'location_excerpt':location,'experience_excerpt':exp_excerpt,
            'required_professional_years':low,'requirements':unique,'generated_by':'auto_fit','notes':notes,
            'unverified_fundamentals_policy':cand['fit']['fundamentals_policy']}
    return packet,[],notes

def _log(db,job_id,digest,outcome,fit,reasons,context):
    db.execute('INSERT OR REPLACE INTO auto_fit_log VALUES(?,?,?,?,?,?)',
               (job_id,digest,job_db.now(),outcome,fit,json.dumps(reasons)))
    db.execute(CONTEXT_DDL)
    db.execute('INSERT OR REPLACE INTO auto_fit_context VALUES(?,?,?)',(job_id,digest,context))
    db.commit()

def review(db,row,cand,config):
    """Review one Verified Naukri job. Returns a small outcome dict."""
    digest=hashlib.sha256((row['jd_text'] or '').encode()).hexdigest()
    context=context_fingerprint(config,cand,row)
    base={'job_id':row['id'],'employer':row['employer'],'title':row['title']}
    packet,reasons,notes=build_packet(row,cand,config)
    if packet is None:
        _log(db,row['id'],digest,'not_eligible',None,reasons,context);return {**base,'outcome':'not_eligible','reasons':reasons}
    verdict=fit_engine.evaluate(dict(row),packet,config,fit_engine.ROOT/'master-resume/Yugal_Teotia_CV.tex')
    if not (verdict['eligible'] or verdict.get('strong_medium_eligible')):
        reasons=verdict['reasons'];_log(db,row['id'],digest,'not_eligible',verdict['fit'],reasons,context)
        return {**base,'outcome':'not_eligible','fit':verdict['fit'],'reasons':reasons}
    provenance=json.loads(row['provenance_json'] or '{}')
    if provenance.get('duplicate_check',{}).get('status')!='clear':
        reasons=['Possible duplicate requisition requires manual resolution']
        _log(db,row['id'],digest,'blocked_duplicate',verdict['fit'],reasons,context);return {**base,'outcome':'blocked_duplicate','reasons':reasons}
    try:
        job_db.bind_verified_portal_id(db,row['id'])
        result=job_db.apply_fit_decision(db,row['id'],packet,config)
    except ValueError as exc:
        _log(db,row['id'],digest,'blocked',verdict['fit'],[str(exc)],context);return {**base,'outcome':'blocked','reasons':[str(exc)]}
    _log(db,row['id'],digest,'queued',result['fit'],[],context)
    return {**base,'outcome':'queued','fit':result['fit'],'strong_medium':bool(result.get('strong_medium_eligible')),'notes':notes,
            'interview_preparation_gaps':result.get('interview_preparation_gaps',[])}

def run(db,config,limit=30):
    cand=candidate.load();db.execute(DDL);db.execute(CONTEXT_DDL);db.commit()
    stale_before=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=cand['fit']['max_jd_age_hours'])).isoformat()
    rows=db.execute("""SELECT * FROM jobs WHERE source IN ('naukri_portal_native','linkedin_portal_native') AND state='Verified' AND availability='Live'
        AND jd_text IS NOT NULL AND checked_at>=? ORDER BY checked_at DESC,id DESC""",(stale_before,)).fetchall()
    excluded={str(x) for x in config['naukri_worker']['excluded_requisition_ids']}
    summary={'considered':0,'queued':0,'not_eligible':0,'blocked':0,'skipped_unchanged':0,'results':[]}
    for row in rows:
        if summary['considered']>=limit:break
        portal=str(json.loads(row['provenance_json'] or '{}').get('portal_job_id') or '')
        if portal in excluded or not review_candidates.eligible(row):continue
        digest=hashlib.sha256(row['jd_text'].encode()).hexdigest()
        context=context_fingerprint(config,cand,row)
        if db.execute('SELECT 1 FROM auto_fit_log l JOIN auto_fit_context c USING(job_id,jd_sha256) WHERE l.job_id=? AND l.jd_sha256=? AND c.context_sha256=?',
                      (row['id'],digest,context)).fetchone():
            summary['skipped_unchanged']+=1;continue
        summary['considered']+=1
        result=review(db,row,cand,config);summary['results'].append(result)
        key='queued' if result['outcome']=='queued' else 'blocked' if result['outcome'].startswith('blocked') else 'not_eligible'
        summary[key]+=1
    return summary
