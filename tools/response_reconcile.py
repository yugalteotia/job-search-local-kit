"""Read-only reconciliation of mailbox/portal message metadata against the tracker.

Input is metadata captured from a signed-in mailbox or portal (records with
sender/subject/date and an optional company, role or requisition id). Nothing is
sent, opened or written to the canonical database: the output is a labelled
report. `exact` and `probable` matches still need review before any tracker
state changes; `ambiguous` and `unmatched` are never auto-applied.
"""
import datetime as dt
import json
import re
from pathlib import Path

import job_db
from local_paths import ROOT

KINDS=[('rejection',r'unfortunately|not (?:be )?(?:moving|proceed)|no longer under consideration|regret to inform|not selected|other candidates|unsuccessful|not shortlisted'),
       ('offer',r'offer letter|pleased to offer|job offer|extend an offer'),
       ('interview',r'interview|schedule a call|screening call|assessment|coding (?:test|challenge)|invite you|availability for'),
       ('acknowledgment',r'thank you for applying|application (?:received|submitted|has been)|track your .* application|your application (?:to|for|at)|we received your application|applied (?:successfully|to)'),
       ('alert',r'job alert|jobs? for you|recommend|now hiring|new jobs|similar jobs|jobs you may|is hiring')]
URGENT={'interview','offer'}

def classify(subject,sender=''):
    text=f'{subject} {sender}'
    for kind,pattern in KINDS:
        if re.search(pattern,text,re.I):return kind
    return 'other'

def _company(record):
    if record.get('company'):return record['company']
    subject=record.get('subject') or ''
    match=re.search(r'(?:\bat|\bto|\bfrom|\bwith)\s+([A-Z][\w&.\- ]{2,40}?)(?:\s*$|[:,!]|\s+(?:is|has|for)\b)',subject)
    return match.group(1).strip() if match else (record.get('sender') or '').strip()

def _tokens(text):
    return {t for t in re.findall(r'[a-z0-9]+',str(text or '').lower()) if len(t)>2}

def match(db,record):
    subject=record.get('subject') or '';company=_company(record);key=job_db.employer(company)
    result={'employer_hint':company,'candidates':[],'confidence':'unmatched'}
    if not key or len(key)<3:return result
    jobs=[dict(r) for r in db.execute('SELECT id,employer,employer_key,title,requisition_id,state,canonical_url FROM jobs')]
    same=[j for j in jobs if j['employer_key']==key or (len(key)>=5 and len(j['employer_key'])>=5 and (key in j['employer_key'] or j['employer_key'] in key))]
    if not same:return result
    text=subject+' '+str(record.get('body_excerpt') or '')
    by_req=[j for j in same if j['requisition_id'] and re.search(r'(?<![A-Za-z0-9])'+re.escape(str(j['requisition_id']))+r'(?![A-Za-z0-9])',text)]
    role=role_from(record)
    by_role=[j for j in same if role and same_role(role,j['title'])]
    if len(by_req)==1:pick,conf=by_req,'exact'
    elif len(by_role)==1:pick,conf=by_role,'probable'
    elif by_role or len(same)>1:pick,conf=(by_role or same),'ambiguous'
    else:pick,conf=same,'company_only'
    result['candidates']=[{k:j[k] for k in ('id','employer','title','requisition_id','state')} for j in pick[:5]]
    result['confidence']=conf
    return result

def reconcile(db,records):
    out=[]
    for record in records:
        kind=record.get('kind') or classify(record.get('subject'),record.get('sender'))
        if kind=='application_acknowledgment':kind='acknowledgment'
        if kind=='alert':
            out.append({'record':{k:record.get(k) for k in ('message_id','sender','subject','date')},'kind':kind,'confidence':'ignored',
                        'note':'Alerts and recommendations are not applications','labels':[]});continue
        found=match(db,record);labels=[]
        top=found['candidates'][0] if len(found['candidates'])==1 else None
        if found['confidence']=='unmatched' and kind=='acknowledgment':
            labels.append('history_gap: acknowledged application is not in the tracker; add to deduplication, never reapply')
        if top and kind=='acknowledgment' and top['state'] in ('SubmissionUnconfirmed','NeedsReview'):
            labels.append('possible_proof_for_uncertain_attempt: review before changing state; never retry')
        if top and kind=='acknowledgment' and top['state'] in ('Verified','Shortlisted','ResumeReady','Prepared','InProgress'):
            labels.append('conflict: mailbox shows an application but tracker state is '+top['state'])
        if kind in ('rejection','interview','offer') and found['confidence'] in ('exact','probable'):
            labels.append('response_candidate: attach as response evidence after review')
        if found['confidence'] in ('ambiguous','company_only') and kind!='other':
            labels.append('ambiguous: several tracker rows share this employer; needs manual match')
        out.append({'record':{k:record.get(k) for k in ('message_id','sender','subject','date')},'kind':kind,
                    'urgent':kind in URGENT and found['confidence']!='unmatched',**found,'labels':labels})
    counts={}
    for item in out:counts[item['confidence']]=counts.get(item['confidence'],0)+1
    return {'generated_at':dt.datetime.now(dt.timezone.utc).isoformat(),'read_only':True,'total':len(out),
            'confidence_counts':counts,'urgent':[i for i in out if i.get('urgent')],'items':out}

ROLE_IN_SUBJECT=re.compile(r'\b(?:application|applied)\s+(?:to|for)\s+(?:the\s+)?(.+?)\s+(?:at|with)\s+[A-Z0-9]',re.I)
ROLE_STOP={'the','and','of','for','a','an','with','in','at','to','your','application'}
GAP_SCHEMA=2

def role_from(record):
    if record.get('role'):return str(record['role'])
    match=ROLE_IN_SUBJECT.search(record.get('subject') or '')
    return match.group(1).strip() if match else None

def role_key(role):
    text=re.sub(r'back[\s-]*end','backend',str(role or '').lower());text=re.sub(r'full[\s-]*stack','fullstack',text)
    return sorted({t for t in re.findall(r'[a-z0-9]+',text) if t not in ROLE_STOP and len(t)>1})

def same_role(left,right):
    a,b=set(role_key(left)),set(role_key(right))
    return len(a)>=2 and len(b)>=2 and len(a&b)/len(a|b)>=0.8

def requisition_from(record):
    if record.get('requisition_id'):return str(record['requisition_id'])
    match=re.search(r'(?<![A-Za-z0-9])(\d{6,})(?![A-Za-z0-9])',(record.get('subject') or '')+' '+str(record.get('body_excerpt') or ''))
    return match.group(1) if match else None

def save_history_gaps(folder,report):
    """Persist acknowledged applications missing from the tracker. Entries keep the role and exact requisition
    when the message shows them; an employer-only entry never blocks a different role."""
    path=Path(folder)/'history-gaps.json'
    try:known={g['message_id']:g for g in json.loads(path.read_text())}
    except (OSError,ValueError):known={}
    for item in report['items']:
        if item['kind']!='acknowledgment' or item['confidence'] not in ('unmatched','company_only','ambiguous'):continue
        key=job_db.employer(item.get('employer_hint'))
        if not key:continue
        message=item['record'].get('message_id') or key+':'+str(item['record'].get('subject'))
        role=role_from(item['record']);req=requisition_from(item['record'])
        known[message]={'schema':GAP_SCHEMA,'message_id':message,'employer_key':key,'subject':item['record'].get('subject'),
                        'date':item['record'].get('date'),'role':role,'requisition_id':req,
                        'scope':'requisition' if req else 'role' if role_key(role) else 'employer_only'}
    path.write_text(json.dumps(list(known.values()),indent=2)+'\n')

def history_gap_match(employer_key,title,root=None,requisition_id=None):
    """The mailbox acknowledgment that blocks this posting, or None.

    exact requisition ID equal -> block; both IDs present but different -> different posting (no block);
    no ID: block only when the acknowledged role is the same role. Employer-only evidence never blocks.
    """
    try:gaps=json.loads((Path(root or ROOT)/'.workflow/reconcile/history-gaps.json').read_text())
    except (OSError,ValueError):return None
    for gap in gaps:
        if gap.get('schema')!=GAP_SCHEMA or gap.get('employer_key')!=employer_key:continue
        if gap.get('requisition_id') and requisition_id:
            if job_db.norm(gap['requisition_id'])==job_db.norm(requisition_id):return gap
            continue
        if gap.get('role') and same_role(gap['role'],title):return gap
    return None

def history_gap_blocks(employer_key,title,root=None,requisition_id=None):
    return history_gap_match(employer_key,title,root,requisition_id) is not None

def load_records(path):
    data=json.loads(Path(path).read_text())
    rows=data.get('records') or data.get('rows') or (data if isinstance(data,list) else [])
    return [dict(r) for r in rows]

def run(path,root=ROOT):
    db=job_db.connect()
    try:report=reconcile(db,load_records(path))
    finally:db.close()
    report['source']=str(path)
    folder=Path(root)/'.workflow/reconcile';folder.mkdir(parents=True,exist_ok=True)
    save_history_gaps(folder,report)
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    (folder/f'{stamp}.json').write_text(json.dumps(report,indent=2,default=str)+'\n')
    (folder/'latest.json').write_text(json.dumps(report,indent=2,default=str)+'\n')
    return {k:report[k] for k in ('total','confidence_counts','source')}|{'urgent':len(report['urgent']),'report':f'.workflow/reconcile/{stamp}.json'}
