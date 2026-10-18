"""Persistent, conservative JD review queue for discovery-only runs."""
import hashlib
import html
import json
import re

import job_db
from source_adapters import plain_html
from experience_ranges import YEARS,number as experience_number

INDIA=re.compile(r'\bindia\b|pune|bengaluru|bangalore|hyderabad|delhi|gurugram|gurgaon|noida|mumbai|chennai',re.I)
ENGINEER=re.compile(r'\b(?:software|backend|back.end|fullstack|full.stack|api)\s+(?:(?:development|platform)\s+)?(?:engineer|developer)\b|\b(?:java|software)\s+(?:development\s+)?(?:engineer|developer)\b|\bjava\b[^\n]{0,50}\b(?:engineer|developer)\b|\bspring\s*boot\b[^\n]{0,35}\b(?:engineer|developer)\b|\bsoftware development engineer\b|\bsde\b',re.I)
NON_TARGET_TITLE=re.compile(r'\b(?:support|data|ai/ml|machine learning|quality|qa|testing|tester|test|sales|deployment|migration|snowflake|value|sap\s+abap)\b',re.I)
JAVA=re.compile(r'\bjava\b',re.I)
SPRING=re.compile(r'spring\s*boot|springboot',re.I)
BACKEND=re.compile(r'backend|back.end|microservice|\brest\b|\bapi\b',re.I)

def verified_portal_id(provenance):
    if provenance.get('verification_method')!='portal_native':return False
    portal=provenance.get('portal');identifier=str(provenance.get('portal_job_id') or '')
    return bool(re.fullmatch(r'[A-Za-z0-9]{8}',identifier) if portal=='cutshort' else
                re.fullmatch(r'\d{5,}',identifier) if portal in ('linkedin','naukri','foundit') else False)

def visible(jd):
    # ATS feeds sometimes contain HTML-escaped HTML; decode the visible text twice.
    return re.sub(r'\s+',' ',plain_html(html.unescape(html.unescape(jd or '')))).strip()

def substantive_jd(jd):
    text=visible(jd)
    # `visible` flattens HTML whitespace, so do not strip title/location with
    # a line-based expression (that used to consume the entire description).
    return len(text)>=250 and bool(re.search(
        r'qualification|responsibilit|requirements|what you.ll do|what you will do|the work you.ll do|must have|you will be responsible|the role:',
        text,re.I))

def eligible(row):
    title=row['title'] or '';jd=row['jd_text'] or ''
    # This is a review-candidate gate, not the High-fit gate. Seniority and
    # stack gaps are evidence to assess from the original JD; filtering them
    # here hid viable/backend-titled postings before review.
    url=row['canonical_url'] or ''
    try:provenance=json.loads(row['provenance_json'] or '{}') if 'provenance_json' in row.keys() else {}
    except (TypeError,ValueError):provenance={}
    portal_identity=verified_portal_id(provenance)
    has_identity=bool(row['requisition_id']) or portal_identity
    return (row['state']=='Verified' and row['availability']=='Live' and bool(row['checked_at'])
            and provenance.get('latest_live_jd_status')!='TitleOnly'
            and bool(jd) and substantive_jd(jd) and has_identity and url.startswith('https://')
            and bool(INDIA.search(row['location'] or ''))
            and bool(ENGINEER.search(title)) and not bool(NON_TARGET_TITLE.search(title)))

def hard_filter_reasons(row):
    title=row['title'] or '';jd=row['jd_text'] or '';reasons=[]
    if row['state']!='Verified' or row['availability']!='Live' or not row['checked_at'] or not jd:
        reasons.append('Exact job JD is not verified live')
    elif not substantive_jd(jd):reasons.append('Exact job posting has no substantive duties or qualification text')
    try:provenance=json.loads(row['provenance_json'] or '{}') if 'provenance_json' in row.keys() else {}
    except (TypeError,ValueError):provenance={}
    if provenance.get('latest_live_jd_status')=='TitleOnly':reasons.append('Latest exact portal page showed no substantive JD')
    portal_identity=verified_portal_id(provenance)
    if not (row['requisition_id'] or portal_identity) or not (row['canonical_url'] or '').startswith('https://'):
        reasons.append('Employer requisition ID or verified portal job ID/HTTPS URL missing')
    if not INDIA.search(row['location'] or ''):reasons.append('Posting location is not established in India')
    if NON_TARGET_TITLE.search(title):reasons.append('Primary role title is outside Java/Spring backend target')
    if not ENGINEER.search(title):reasons.append('Title does not indicate a target software engineering role')
    return reasons

def coverage(db):
    rows=db.execute("SELECT * FROM jobs WHERE state='Verified' AND availability='Live' AND jd_text IS NOT NULL").fetchall()
    metrics={'verified_live_postings':len(rows),'with_requisition_and_https_url':0,
             'with_verified_identity_and_https_url':0,'portal_native_verified':0,
             'india_location':0,'target_engineering_title':0,'substantive_original_jd':0,
             'substantive_job_jd':0,'reviewable':0,
             'excluded':{'missing_identity':0,'location_not_india':0,'title_outside_target':0,
                         'no_substantive_jd':0,'latest_jd_unreadable':0}}
    for row in rows:
        try:provenance=json.loads(row['provenance_json'] or '{}')
        except (TypeError,ValueError):provenance={}
        portal_identity=verified_portal_id(provenance)
        employer_identity=bool(row['requisition_id'])
        if not (employer_identity or portal_identity) or not (row['canonical_url'] or '').startswith('https://'):
            metrics['excluded']['missing_identity']+=1;continue
        metrics['with_verified_identity_and_https_url']+=1
        if employer_identity:metrics['with_requisition_and_https_url']+=1
        if provenance.get('latest_live_jd_status')=='TitleOnly':
            metrics['excluded']['latest_jd_unreadable']+=1;continue
        if portal_identity:metrics['portal_native_verified']+=1
        if not INDIA.search(row['location'] or ''):
            metrics['excluded']['location_not_india']+=1;continue
        metrics['india_location']+=1
        if not ENGINEER.search(row['title'] or '') or NON_TARGET_TITLE.search(row['title'] or ''):
            metrics['excluded']['title_outside_target']+=1;continue
        metrics['target_engineering_title']+=1
        if not substantive_jd(row['jd_text']):
            metrics['excluded']['no_substantive_jd']+=1;continue
        metrics['substantive_job_jd']+=1
        if employer_identity:metrics['substantive_original_jd']+=1
        metrics['reviewable']+=1
    return metrics

def priority(row):
    title=row['title'] or '';jd=visible(row['jd_text']);score=0
    # Title is a stronger first-pass signal than generic ATS boilerplate.
    score+=45 if re.search(r'java',title,re.I) else 0
    score+=40 if re.search(r'backend|back.end|api',title,re.I) else 0
    score+=30 if re.search(r'engineer|developer|sde',title,re.I) else 0
    score+=15 if re.search(r'full.?stack',title,re.I) else 0
    score+=30 if JAVA.search(jd) else 0
    score+=35 if SPRING.search(jd) else 0
    score+=20 if BACKEND.search(jd) else 0
    # Prefer requirements-language evidence over arbitrary numbers elsewhere
    # in company boilerplate or benefits text.
    qualification=re.split(r'the qualifications you need:|qualifications:|requirements:',jd,flags=re.I)
    required=qualification[1][:4000] if len(qualification)>1 else jd[:4000]
    years=[experience_number(x.group(1)) for x in YEARS.finditer(required)]
    if years and min(years)<=3:score+=30
    if years and min(years)>3:score-=25
    if re.search(r'\b(?:staff|principal)\b',title,re.I):score-=40
    elif re.search(r'\b(?:senior|lead)\b',title,re.I):score-=15
    # Stable recency tiebreak from the last original check.
    stamp=row['checked_at'] or row['discovered_at'] or ''
    try:
        import datetime as dt
        checked=dt.datetime.fromisoformat(stamp.replace('Z','+00:00'))
        age_hours=max(0,(dt.datetime.now(dt.timezone.utc)-checked.astimezone(dt.timezone.utc)).total_seconds()/3600)
        score+=max(0,10-age_hours/24)
    except (ValueError,TypeError):pass
    return score

def enqueue(db,job_ids):
    added=0
    for job_id in job_ids:
        row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not row:continue
        if not eligible(row):
            db.execute("UPDATE review_queue SET state='ExcludedByHardFilter' WHERE job_id=?",(job_id,))
            continue
        digest=hashlib.sha256(row['jd_text'].encode()).hexdigest()
        prior=db.execute('SELECT * FROM review_queue WHERE job_id=?',(job_id,)).fetchone()
        if prior and prior['state']=='Reviewed':
            previous=json.loads(prior['review_json'] or '{}')
            if previous.get('jd_sha256')==digest and previous.get('review_version')==8:continue
        db.execute("INSERT INTO review_queue(job_id,priority,queued_at,state) VALUES(?,?,?,'Pending') ON CONFLICT(job_id) DO UPDATE SET priority=excluded.priority,queued_at=excluded.queued_at,state='Pending',reviewed_at=NULL,review_json='{}'",
                   (job_id,priority(row),job_db.now()))
        added+=1
    return added

def _excerpt(text,pattern,width=145):
    match=pattern.search(text)
    if not match:return None
    start=max(0,match.start()-35);end=min(len(text),match.end()+width)
    return text[start:end].strip()

def assess(row,config):
    text=visible(row['jd_text']);title=row['title'] or ''
    qualification=re.split(r'the qualifications you need:|qualifications:|requirements:',text,flags=re.I)
    required=qualification[1][:4000] if len(qualification)>1 else text[:4000]
    required=re.split(r'highly desired:|nice to have:|what .* can offer',required,flags=re.I)[0]
    # Hard experience requirements can precede the Requirements heading.
    experience_text=qualification[0]+' '+required if len(qualification)>1 else required
    years=[(experience_number(m.group(1)),m.group(0),m.start()) for m in YEARS.finditer(experience_text)]
    minimum=max((x[0] for x in years),default=None)
    year_excerpt=_excerpt(experience_text,YEARS,110) if years else None
    java=bool(JAVA.search(required) or JAVA.search(title))
    spring=bool(SPRING.search(required) or SPRING.search(title))
    backend=bool(BACKEND.search(required))
    tech_excerpt=_excerpt(required,re.compile(r'\bjava\b.{0,90}?(?:spring\s*boot|spring|hibernate)|spring\s*boot',re.I),90)
    reasons=[]
    if not java:reasons.append('Java is not established as an essential qualification')
    if not backend:reasons.append('Substantial backend work is not established in qualifications')
    if minimum is not None and minimum>config['high_fit']['maximum_required_professional_years']:
        reasons.append(f'Required experience starts at {minimum:g} years, above the {config["high_fit"]["maximum_required_professional_years"]:g}-year High-fit gate')
    if re.search(r'\bSAP\s+ABAP\b',title,re.I):reasons.append('SAP ABAP role; no matching production work evidence in generic master')
    if not years:reasons.append('Required experience band is not explicit in the qualification excerpt')
    if re.search(r'\bstaff\b|\bprincipal\b',title,re.I) or (minimum is not None and minimum>=5) or not java:
        verdict='Low'
    elif not spring or (minimum is not None and minimum>config['high_fit']['maximum_required_professional_years']):
        verdict='Medium'
    else:
        verdict='Unresolved';reasons.append('Essential requirements need complete original-JD and master-work excerpt review before High')
    return {'fit':verdict,'eligible_for_resume':False,'review_version':8,'jd_sha256':hashlib.sha256(row['jd_text'].encode()).hexdigest(),
            'original_page_checked_at':row['checked_at'],'reviewed_at':job_db.now(),'experience_minimum':minimum,
            'evidence':{'location':row['location'],'experience':year_excerpt,'technology':tech_excerpt,
                        'role':_excerpt(text,re.compile(r'backend engineer|software engineer|SAP ABAP',re.I),100)},
            'reasons':reasons}

def consume(db,config,limit=20,per_company=5,allowed_job_ids=None):
    results=[];taken={}
    allowed=set(allowed_job_ids) if allowed_job_ids is not None else None
    # Read the whole pending set so a large feed from one employer cannot hide
    # other employers beyond an arbitrary SQL LIMIT. SQL ordering still ranks
    # fit relevance first; the per-company cap is applied in Python.
    queue=db.execute("SELECT q.job_id,j.employer_key,j.source FROM review_queue q JOIN jobs j ON j.id=q.job_id WHERE q.state='Pending' ORDER BY q.priority DESC,q.queued_at,q.job_id").fetchall()
    for item in queue:
        if len(results)>=limit:break
        if allowed is not None and item['job_id'] not in allowed:continue
        company=item['employer_key']
        if taken.get(company,0)>=per_company:continue
        row=db.execute('SELECT * FROM jobs WHERE id=?',(item['job_id'],)).fetchone()
        if not row or not eligible(row):
            db.execute("UPDATE review_queue SET state='Skipped' WHERE job_id=?",(item['job_id'],));continue
        result=assess(row,config)
        db.execute("UPDATE review_queue SET state='Reviewed',reviewed_at=?,review_json=? WHERE job_id=?",
                   (result['reviewed_at'],json.dumps(result),item['job_id']))
        db.execute('UPDATE jobs SET fit=?,fit_json=?,updated_at=? WHERE id=?',
                   ('Unrated' if result['fit']=='Unresolved' else result['fit'],json.dumps({'review':result}),job_db.now(),item['job_id']))
        results.append({'job_id':item['job_id'],'company':row['employer'],'role':row['title'],
                        'location':row['location'],'direct_url':row['canonical_url'],**result})
        taken[company]=taken.get(company,0)+1
    return results
