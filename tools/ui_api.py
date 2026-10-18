"""UI data and actions: sanitized projections of canonical state. No secrets, answers or profile text leave here."""
import json
import re
import subprocess
from pathlib import Path

import candidate
import control
import core_run
import job_config
import job_db
import progress
import quick_status
from local_paths import ROOT
from portals import registry

TRACKER_COLUMNS=['Company','Role','Location','Job ID','Fit','Status','Status date','Source','Direct link','Submission date','Next step','Responses','Resume version']
SLUG=re.compile(r'^[a-z0-9-]{3,120}$')
FILE=re.compile(r'^[A-Za-z0-9_.-]{1,120}\.(?:json|pdf|png)$')
NEVER=('presubmit',)   # pre-submit packets are internal; outcome/confirmation files are shown instead

def _scrubber():
    who=candidate.load()['identity']
    secrets_=[who['phone'],who['phone'][3:],who['email'],who['linkedin'],who['github']]
    pattern=re.compile('|'.join(re.escape(x) for x in secrets_ if x),re.I)
    def redact(text):
        text=pattern.sub('[redacted]',text)
        text=re.sub(r'(?i)(?:₹|INR\s*)?\d+(?:\.\d+)?\s*LPA\b','[redacted compensation]',text)
        return re.sub(r'(?i)\bLPA\b','[redacted pay unit]',text)
    return redact

def scrub(value,_s=None):
    _s=_s or _scrubber()
    if isinstance(value,str):return _s(value)
    if isinstance(value,list):return [scrub(x,_s) for x in value]
    if isinstance(value,dict):return {k:scrub(v,_s) for k,v in value.items()}
    return value

def _timer():
    out=subprocess.run(['systemctl','--user','show',core_run.control.UNIT+'.timer','--property=ActiveState','--property=NextElapseUSecRealtime'],
                       capture_output=True,text=True).stdout
    info=dict(line.split('=',1) for line in out.splitlines() if '=' in line)
    return {'active':info.get('ActiveState')=='active','next':info.get('NextElapseUSecRealtime') or None}

def _db():
    db=job_db.connect();job_db.initialize(db);return db

def status():
    config=job_config.load();db=_db()
    try:
        data=quick_status.collect(db,ROOT,config)
        caps=registry.capabilities(db,config,persist=False)
        providers={p:{'manual_live_enabled':job_config.live_provider_enabled(config,p) and caps[p]['capability']=='ApplyReady',
            'timer_live_enabled':job_config.live_provider_enabled(config,p,timer=True) and caps[p]['capability']=='ApplyReady',
            'configured_live':config['portal_workers'][p]['apply_enabled'],
            'max_submissions_per_run':config['portal_workers'][p].get('max_submissions_per_run',1),
            'max_new_attempts_per_run':config['portal_workers'][p].get('max_new_attempts_per_run',config['portal_workers'][p].get('max_submissions_per_run',1)),
            'reason':caps[p]['reason']} for p in ('naukri','linkedin')}
    finally:db.close()
    running=core_run.lock_held()
    return scrub({'generated_at':data['generated_at'],'control':{'paused':control.is_paused()},
        'timer':{**_timer(),'schedule_enabled':config['schedule']['enabled']},
        'run':{'running':running,'current':progress.read()['run']},
        'lane':{'naukri_worker_enabled':config['naukri_worker']['enabled'],'global_submissions_enabled':config['submissions']['enabled'],
                'manual_live_enabled':any(v['manual_live_enabled'] for v in providers.values()),
                'timer_live_enabled':any(v['timer_live_enabled'] for v in providers.values()),'providers':providers},
        'counts':{'applied':len(data['applied']),'needs_answer':len(data['needs_answer']),'uncertain':len(data['uncertain']),
                  'urgent':len(data['urgent'])},
        'queue':data['queue'],'ready':data['ready'],'near_misses':data['near_misses'],'last_run':data['last_run'],
        'mail':data['mail'],'portals':caps})

def handoffs():
    config=job_config.load();db=_db()
    try:data=quick_status.collect(db,ROOT,config)
    finally:db.close()
    def slim(rows,keys):return [{k:r.get(k) for k in keys} for r in rows]
    return scrub({'needs_answer':slim(data['needs_answer'],['id','employer','title','canonical_url','slug','question','reason','status_date']),
        'uncertain':slim(data['uncertain'],['id','employer','title','canonical_url','slug','pending_question','status_date']),
        'urgent':slim(data['urgent'],['id','employer','title','canonical_url','state','status_date']),
        'applied':slim(data['applied'],['id','employer','title','canonical_url','slug','proof','status_date','outcome_label']),
        'mail':data['mail']})

def tracker(view='quick'):
    db=_db()
    try:rows=job_db.tracker_rows(db)
    finally:db.close()
    active=('shortlisted','inprogress','prepared','resumeready','needsanswer','applied','interview','offer','submissionunconfirmed','needsreview','preparing')
    if view!='detailed':rows=[r for r in rows if str(r.get('Status') or '').lower().replace(' ','') in active]
    return scrub({'view':view,'columns':TRACKER_COLUMNS,'rows':[{c:r.get(c) for c in TRACKER_COLUMNS} for r in rows]})

def portals():
    config=job_config.load();db=_db()
    try:caps=registry.capabilities(db,config,persist=False)
    finally:db.close()
    sources=[{'section':section,'name':name,'enabled':rec['enabled'],'last_successful_fetch':rec['last_successful_fetch']}
             for section in ('sources','employer_boards') for name,rec in config[section].items()]
    return scrub({'capabilities':caps,'sources':sources,
        'apply_enabled':{k:v['apply_enabled'] for k,v in config['portal_workers'].items()},
        'naukri_worker_enabled':config['naukri_worker']['enabled'],
        'note':'Sign-in is done manually in each portal profile; the UI never handles credentials.'})

def evidence_list(slug):
    if not SLUG.match(slug):raise ValueError('Invalid job')
    folder=(ROOT/'jobs'/slug)
    if not folder.is_dir():return {'files':[]}
    return {'files':sorted(p.name for p in folder.iterdir() if p.is_file() and not p.is_symlink() and FILE.match(p.name)
                           and not any(n in p.name for n in NEVER))}

def evidence_file(slug,name):
    if not SLUG.match(slug) or not FILE.match(name) or any(n in name for n in NEVER):raise ValueError('Invalid evidence path')
    path=(ROOT/'jobs'/slug/name)
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to((ROOT/'jobs').resolve()):raise FileNotFoundError(name)
    kind={'.json':'application/json','.pdf':'application/pdf','.png':'image/png'}[path.suffix]
    data=path.read_bytes()
    if kind=='application/json':data=json.dumps(scrub(json.loads(data.decode())),indent=2).encode()
    return kind,data

def run_now(dry_run):
    return core_run.start_detached(dry_run=bool(dry_run),trigger='ui')

def pause():return control.pause()
def resume():return control.resume(job_config.load())

def toggle_source(section,name,enabled):
    job_config.set_source_enabled(section,name,enabled);return {'ok':True}

def set_lane(portal,enabled):
    """Use the same validated provider controls as CLI and timer."""
    if portal=='naukri':
        job_config.set_naukri_mode(bool(enabled),scheduled=bool(enabled and job_config.load()['schedule']['application_enabled']))
        return {'ok':True}
    if portal=='linkedin':
        if enabled:
            db=_db()
            try:
                if not job_db.portal_path_verified(db,'linkedin'):raise ValueError('LinkedIn application proof is missing or blocked')
            finally:db.close()
        job_config.set_linkedin_mode(bool(enabled),scheduled=bool(enabled and job_config.load()['portal_workers']['linkedin'].get('scheduled_apply_enabled')))
        return {'ok':True}
    job_config.set_apply_enabled(portal,enabled);return {'ok':True}

def ack_alert(slug):
    import urgent_alerts,workflow
    return urgent_alerts.acknowledge(workflow,'sqlite-'+slug if not slug.startswith('sqlite-') else slug)
