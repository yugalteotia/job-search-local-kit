"""Per-job urgent handoffs with local desktop and opt-in Pushover transport."""
import datetime as dt
import contextlib
import fcntl
import getpass
import json
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

from local_paths import ROOT
from private_playwright import PRIVATE

STATE=ROOT/'.workflow/alerts'
PHONE=PRIVATE/'pushover.json'
TITLE='Job search: action needed'
REPEAT=dt.timedelta(hours=6)
ACTIONS={
    'unknown_required_answer':'Review a required application question',
    'captcha':'Complete CAPTCHA in browser',
    'login':'Sign in to the application portal',
    'expiring_high_fit':'Review an expiring High-fit opportunity',
}

def utcnow(): return dt.datetime.now(dt.timezone.utc)

def phone_config():
    if not PHONE.is_file(): return None
    for path,folder in ((PRIVATE,True),(PHONE,False)):
        info=path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077 or (stat.S_ISDIR(info.st_mode)!=folder):
            raise ValueError('Private Pushover configuration requires owner-only 0700 directory and 0600 file.')
    return json.loads(PHONE.read_text())

def setup_phone():
    if not sys.stdin.isatty(): raise ValueError('Run phone setup directly in a terminal with hidden input.')
    token=getpass.getpass('Pushover application API token (hidden): ').strip()
    user=getpass.getpass('Pushover user key (hidden): ').strip()
    if not re.fullmatch(r'[A-Za-z0-9]{30}',token) or not re.fullmatch(r'[A-Za-z0-9]{30}',user):
        raise ValueError('Pushover keys must each be 30 alphanumeric characters; nothing saved.')
    if PRIVATE.is_symlink(): raise ValueError('Private directory is a symlink')
    PRIVATE.mkdir(mode=0o700,parents=True,exist_ok=True);PRIVATE.chmod(0o700)
    fd,tmp=tempfile.mkstemp(dir=PRIVATE)
    try:
        with os.fdopen(fd,'w') as file: json.dump({'token':token,'user':user,'enabled':False},file)
        os.replace(tmp,PHONE)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    return {'configured':True,'enabled':False,'next':'Run workflow alerts test-phone; acknowledge the test in Pushover, then workflow alerts confirm-phone.'}

def api_post(endpoint,payload):
    config=phone_config()
    if not config: return {'ok':False,'reason':'Pushover not configured'}
    body=urllib.parse.urlencode({'token':config['token'],**payload}).encode()
    request=urllib.request.Request('https://api.pushover.net/1/'+endpoint,data=body,method='POST')
    try:
        with urllib.request.urlopen(request,timeout=10) as response:
            result=json.loads(response.read(4096))
        return {'ok':result.get('status')==1,'receipt':result.get('receipt')}
    except Exception:
        # Never let a network exception render a credential-bearing request URL.
        return {'ok':False,'reason':'Pushover API request failed; credentials were not logged'}

def receipt_status(receipt):
    config=phone_config()
    if not config or not re.fullmatch(r'[A-Za-z0-9]{30}',receipt or ''):
        return {'ok':False}
    url='https://api.pushover.net/1/receipts/'+receipt+'.json?'+urllib.parse.urlencode({'token':config['token']})
    try:
        with urllib.request.urlopen(url,timeout=10) as response:
            result=json.loads(response.read(4096))
        return {'ok':result.get('status')==1,'acknowledged':result.get('acknowledged')==1,
                'expired':result.get('expired')==1}
    except Exception:
        return {'ok':False,'reason':'Pushover receipt check failed; credentials were not logged'}

def cancel_phone(receipt):
    if not re.fullmatch(r'[A-Za-z0-9]{30}',receipt or ''): return {'ok':False}
    return api_post('receipts/'+receipt+'/cancel.json',{})

def test_phone(w):
    if not phone_config(): return {'configured':False,'sent':False,'reason':'Run workflow alerts phone-setup locally first'}
    result=api_post('messages.json',{'user':phone_config()['user'],'title':'Job search test',
             'message':'Acknowledge this test notification in Pushover','priority':2,'retry':60,'expire':300})
    if result.get('ok') and result.get('receipt'):
        w.write_json(STATE/'phone-test.json',{'receipt':result['receipt'],'queued_at':utcnow().isoformat(),'confirmed':False})
    return {'configured':True,'queued':bool(result.get('ok')),'received':False,
            'next':'Acknowledge on phone, then run workflow alerts confirm-phone' if result.get('ok') else result.get('reason','Pushover did not accept test')}

def confirm_phone(w):
    test=w.read_json(STATE/'phone-test.json',{})
    if not test.get('receipt'): return {'enabled':False,'reason':'No test notification was queued'}
    status=receipt_status(test['receipt'])
    if not status.get('acknowledged'):
        return {'enabled':False,'reason':'Test receipt not acknowledged on phone; delivery remains unverified'}
    config=phone_config();config['enabled']=True
    fd,tmp=tempfile.mkstemp(dir=PRIVATE)
    try:
        with os.fdopen(fd,'w') as file: json.dump(config,file)
        os.replace(tmp,PHONE)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    w.write_json(STATE/'phone-test.json',{'confirmed':True,'confirmed_at':utcnow().isoformat()})
    return {'enabled':True,'proof':'Pushover test receipt acknowledged by phone'}

def job_link(url):
    p=urllib.parse.urlsplit(str(url or ''))
    if p.scheme!='https' or not p.netloc or p.username or p.password:
        raise ValueError('Urgent job link requires an HTTPS original listing')
    return urllib.parse.urlunsplit((p.scheme,p.netloc,p.path,'',''))

def desktop(title,body):
    notice=subprocess.run(['notify-send','--app-name=Job search','--urgency=critical',
                           '--expire-time=0','--print-id',title,body],capture_output=True,text=True,timeout=10)
    sound=subprocess.run(['canberra-gtk-play','-i','dialog-error','-d','Job search urgent handoff'],
                         capture_output=True,timeout=10)
    identifier=notice.stdout.strip() if notice.returncode==0 else ''
    return {'notification_sent':notice.returncode==0,'sound_played_command':sound.returncode==0,
            'notification_id':int(identifier) if identifier.isdecimal() else None}

def test_desktop():
    return desktop('Job search alert test','Desktop notification and sound test. Dismiss this test after checking it.')

def _state(w): return w.read_json(STATE/'state.json',{})

def _save(w,data): w.write_json(STATE/'state.json',data)

@contextlib.contextmanager
def _locked_state():
    STATE.mkdir(parents=True,exist_ok=True)
    with (STATE/'alerts.lock').open('a') as file:
        fcntl.flock(file,fcntl.LOCK_EX)
        yield

def status(w):
    config=phone_config()
    state=_state(w)
    return {'phone_configured':bool(config),'phone_enabled':bool(config and config.get('enabled')),
            'phone_test_confirmed':bool(w.read_json(STATE/'phone-test.json',{}).get('confirmed')),
            'open_jobs':[{ 'slug':slug,'kind':r.get('kind'),'acknowledged':r.get('acknowledged',False)} for slug,r in state.items() if not r.get('resolved')]}

def _close_desktop(identifier):
    if not isinstance(identifier,int): return False
    result=subprocess.run(['gdbus','call','--session','--dest','org.freedesktop.Notifications',
        '--object-path','/org/freedesktop/Notifications','--method',
        'org.freedesktop.Notifications.CloseNotification',str(identifier)],capture_output=True,timeout=5)
    return result.returncode==0

def acknowledge(w,slug):
    with _locked_state():
        return _acknowledge_locked(w,slug)

def _acknowledge_locked(w,slug):
    state=_state(w);record=state.get(slug)
    if not record: raise ValueError('No alert for this job')
    record['acknowledged']=True;record['acknowledged_at']=utcnow().isoformat()
    desktop_closed=_close_desktop(record.get('desktop_id'))
    phone_canceled=None
    if record.get('phone_receipt'):
        phone_canceled=cancel_phone(record['phone_receipt']).get('ok')
        record['phone_cancel_pending']=not phone_canceled
    _save(w,state)
    return {'acknowledged':True,'desktop_close_requested':desktop_closed,'phone_retry_canceled':phone_canceled,
            'note':'Acknowledgment mutes repeats; the required answer or login remains outstanding'}

def resolve(w,slug,proof_path):
    """Clear a blocker only with a saved, redacted fact/handoff verification."""
    w.check_pause()
    review=w.read_json(proof_path,{})
    if review.get('resolved') is not True or review.get('next_status') not in ('Shortlisted','Prepared'):
        raise ValueError('Resolution needs confirmed handoff and Shortlisted or Prepared next status')
    artifact=(w.ROOT/str(review.get('evidence',''))).resolve()
    if not artifact.is_relative_to(w.ROOT.resolve()) or not artifact.is_file():
        raise ValueError('Resolution needs existing local redacted evidence')
    listing=w.read_json(w.job_folder(slug)/'listing.json',{})
    identity={'Company':listing.get('company'),'Role':listing.get('role'),'Job ID':listing.get('job_id'),'Direct link':listing.get('url')}
    row=next((r for r in w.tracker_rows()[3] if w.key(r)==w.key(identity)),{})
    if row.get('Status')!='Needs Answer': raise ValueError('Job is not awaiting an answer')
    with _locked_state():
        state=_state(w);record=state.get(slug,{})
        if not record or record.get('blocker_id')!=review.get('blocker_id'):
            raise ValueError('Resolution does not match current blocker')
        w.upsert({**identity,'Status':review['next_status'],'Next step':'Resume this verified job in the next available run'},'verified handoff resolved')
        record['resolved']=True;record['acknowledged']=True;record['resolved_at']=utcnow().isoformat()
        _close_desktop(record.get('desktop_id'))
        if record.get('phone_receipt'): record['phone_cancel_pending']=not cancel_phone(record['phone_receipt']).get('ok')
        state[slug]=record;_save(w,state)
    w.event('needs_answer_resolved',slug=slug,status=review['next_status'])
    return {'job':slug,'status':review['next_status'],'resolved':True}

def mark(w,slug,request_path):
    w.check_pause()
    job=w.job_folder(slug);listing=w.read_json(job/'listing.json',{})
    if not listing or listing.get('fit')!='High' or listing.get('original_verified') is not True:
        raise ValueError('Needs Answer alerts require an original-verified High-fit job')
    request=w.read_json(request_path,{})
    if set(request)-{'kind','blocker_id','deadline','deadline_evidence'}:
        raise ValueError('Blocker packet may not contain answers, form contents or arbitrary text')
    kind=request.get('kind')
    if kind not in ACTIONS or not re.fullmatch(r'[a-f0-9]{16,64}',str(request.get('blocker_id',''))):
        raise ValueError('Use a supported blocker kind and non-sensitive stable blocker hash')
    link=job_link(listing.get('url'))
    deadline=None
    if request.get('deadline'):
        try: deadline=dt.datetime.fromisoformat(request['deadline'].replace('Z','+00:00'))
        except ValueError: raise ValueError('Deadline needs an exact observed timestamp with timezone')
        proof=(w.ROOT/str(request.get('deadline_evidence',''))).resolve()
        if deadline.tzinfo is None or not proof.is_relative_to(w.ROOT.resolve()) or not proof.is_file():
            raise ValueError('Deadline needs local original-listing evidence')
        if deadline<=utcnow(): raise ValueError('Deadline passed; reverify listing status instead of alerting')
    if kind=='expiring_high_fit' and not deadline:
        raise ValueError('Expiring High-fit alert requires verified deadline evidence')
    urgent=kind in ('captcha','login') or bool(deadline and deadline-utcnow()<=dt.timedelta(hours=24))
    action=ACTIONS[kind]
    identity={'Company':listing.get('company'),'Role':listing.get('role'),'Job ID':listing.get('job_id'),'Direct link':listing.get('url')}
    current=next((r for r in w.tracker_rows()[3] if w.key(r)==w.key(identity)),{})
    if any(s in str(current.get('Status') or '').lower() for s in ('applied','submission unconfirmed','rejected','closed','expired','withdrawn')):
        raise ValueError('Already actioned or terminal job; do not reapply or alert')
    packet={'kind':kind,'blocker_id':request['blocker_id'],'action_needed':action,
            'urgent':urgent,'deadline':deadline.isoformat() if deadline else None,'job_link':link,'recorded_at':utcnow().isoformat()}
    w.write_json(job/'needs_answer.json',packet)
    w.upsert({**identity,'Status':'Needs Answer','Next step':action+'; review job link'},'per-job required handoff')
    with _locked_state():
        return _mark_locked(w,slug,request,packet,identity,current)

def _mark_locked(w,slug,request,packet,identity,current):
    kind=packet['kind'];action=packet['action_needed'];link=packet['job_link'];urgent=packet['urgent']
    state=_state(w);record=state.get(slug,{})
    if record.get('blocker_id')!=request['blocker_id']:
        if record.get('desktop_id'): _close_desktop(record['desktop_id'])
        if record.get('phone_receipt'): cancel_phone(record['phone_receipt'])
        record={'blocker_id':request['blocker_id'],'kind':kind,'acknowledged':False}
    output={'job':slug,'status':'Needs Answer','urgent':urgent,'action_needed':action,'other_jobs_unchanged':True}
    if urgent and not record.get('acknowledged'):
        if record.get('phone_receipt') and not record.get('phone_acknowledged'):
            # Check at most once per run, never in a retry loop.
            status=receipt_status(record['phone_receipt'])
            if status.get('acknowledged'):
                record['phone_acknowledged']=True;record['acknowledged']=True
                _close_desktop(record.get('desktop_id'))
        last=dt.datetime.fromisoformat(record['last_desktop_sent']) if record.get('last_desktop_sent') else None
        if not record.get('acknowledged') and (last is None or utcnow()-last>=REPEAT):
            result=desktop(TITLE,action+'\n'+link+'\nMute repeats: python3 workflow alert-ack '+slug)
            record['last_desktop_sent']=utcnow().isoformat();record['desktop_id']=result['notification_id']
            output['desktop']= {k:v for k,v in result.items() if k!='notification_id'}
        config=phone_config()
        if config and config.get('enabled') and not record.get('phone_receipt') and not record.get('acknowledged'):
            sent=api_post('messages.json',{'user':config['user'],'title':TITLE,'message':action,
                        'url':link,'url_title':'Open job','priority':2,'retry':300,'expire':1800})
            if sent.get('ok') and sent.get('receipt'):
                record['phone_receipt']=sent['receipt'];record['phone_sent_at']=utcnow().isoformat()
            output['phone_queued']=bool(sent.get('ok'))
    state[slug]=record;_save(w,state)
    w.event('needs_answer',slug=slug,kind=kind,urgent=urgent,desktop=output.get('desktop'),phone_queued=output.get('phone_queued'))
    return output


def resolve_sqlite_confirmed_blocker(db,job_id,observed_proof_path=None):
    """Close a job's alert only after its durable exact-job claim is confirmed."""
    row=db.execute('''SELECT j.slug,j.state,j.requisition_id,a.state AS attempt_state,a.proof_path,a.proof_sha256
        FROM jobs j LEFT JOIN submission_attempts a ON a.job_id=j.id WHERE j.id=?''',(job_id,)).fetchone()
    if not row or row['state']!='Applied':return False
    row=dict(row)
    if row['attempt_state']!='confirmed':
        if row['attempt_state'] is not None or not observed_proof_path:return False
        ev=db.execute("SELECT sha256 FROM evidence WHERE job_id=? AND path=? AND kind IN ('observed_application','submission_confirmation')",(job_id,observed_proof_path)).fetchone()
        if not ev:return False
        row.update(proof_path=observed_proof_path,proof_sha256=ev['sha256'])
    proof=(ROOT/str(row['proof_path'])).resolve()
    if not proof.is_relative_to((ROOT/'jobs'/row['slug']).resolve()) or not proof.is_file():return False
    if hashlib.sha256(proof.read_bytes()).hexdigest()!=row['proof_sha256']:return False
    if row['attempt_state'] is None:
        packet=json.loads(proof.read_text())
        if not (packet.get('history_full') is True and packet.get('exact_history_id_match') is True and
                str(packet.get('requisition_id'))==str(row['requisition_id'])):return False
    with _locked_state():
        state=_state(_StateIO());record=state.get('sqlite-'+row['slug'])
        if not record:return False
        record.update(resolved=True,resolved_at=utcnow().isoformat(),resolution_evidence=row['proof_path'],resolution='Exact job application confirmed')
        _save(_StateIO(),state)
        try:_close_desktop(record.get('desktop_id'))
        except (OSError,subprocess.SubprocessError):pass
    return True

def sync_sqlite_blockers(db, config):
    """Notify for canonical, job-bound High/Medium Naukri handoffs.

    The persistent alert state supplies cooldown and the existing alert-ack
    command. No answer, profile field, request body, or query string is sent.
    """
    rows = db.execute("""SELECT j.id,j.slug,j.canonical_url,j.status_date,j.requisition_id,j.state,
        a.attempt_id,a.state AS attempt_state FROM jobs j LEFT JOIN submission_attempts a ON a.job_id=j.id
        WHERE j.fit IN ('High','Medium') AND j.source IN ('naukri_portal_native','linkedin_portal_native') AND (
          (j.state='NeedsAnswer' AND (a.attempt_id IS NULL OR a.state='incomplete')) OR
          (j.state='SubmissionUnconfirmed' AND a.state='uncertain') OR
          (j.state IN ('ResumeReady','Prepared') AND a.attempt_id IS NULL))
        ORDER BY j.status_date,j.id""").fetchall()
    outcomes=[]
    with _locked_state():
        state=_state(_StateIO())
        for row in rows:
            key='sqlite-'+str(row['slug'])
            uncertain=row['state']=='SubmissionUnconfirmed'
            technical=False;outcome={}
            if row['state'] in ('ResumeReady','Prepared'):
                folder=ROOT/'jobs'/row['slug'];path=folder/'naukri-native-outcome.json'
                if not path.exists():path=folder/'linkedin-easy-apply-outcome.json'
                try:outcome=json.loads(path.read_text())
                except (OSError,ValueError):continue
                if outcome.get('status')!='Blocked' or outcome.get('attempt_id'):continue
                technical=True
            fingerprint=str(row['id'])+':'+str(row['status_date'])+(':'+str(row['attempt_id']) if uncertain else '')
            if technical:
                fingerprint+=':'+hashlib.sha256(json.dumps({k:outcome.get(k) for k in ('stage','error_message','error_type','reason')},sort_keys=True).encode()).hexdigest()
            record=state.get(key,{})
            if record.get('blocker_id')!=fingerprint:
                record={'blocker_id':fingerprint,'kind':'uncertain_submission' if uncertain else 'application_worker_blocked' if technical else 'unknown_required_answer',
                        'acknowledged':False,'resolved':False}
            if record.get('acknowledged'):
                outcomes.append({'job_id':row['id'],'status':'acknowledged'});state[key]=record;continue
            last=dt.datetime.fromisoformat(record['last_desktop_sent']) if record.get('last_desktop_sent') else None
            cooldown=dt.timedelta(minutes=config['alerts']['repeat_cooldown_minutes'])
            if last and utcnow()-last<cooldown:
                outcomes.append({'job_id':row['id'],'status':'cooldown'});state[key]=record;continue
            link=job_link(row['canonical_url'])
            action=('Application outcome is uncertain. Check this exact job and application history; do not retry.'
                    if uncertain else 'Review required application answer or browser handoff')
            if technical:
                action='Application worker blocked at '+str(outcome.get('stage') or 'preflight')+': '+str(outcome.get('error_message') or outcome.get('reason') or outcome.get('error_type') or 'Inspect saved outcome')
            elif not uncertain:
                folder=ROOT/'jobs'/row['slug']
                path=folder/'linkedin-easy-apply-outcome.json'
                if not path.exists():path=folder/'naukri-native-outcome.json'
                try:
                    question=json.loads(path.read_text()).get('question')
                    if question:action+=': '+question
                except (OSError,ValueError):pass
            item={'job_id':row['id'],'status':'pending','link':link}
            if config['alerts']['desktop_enabled']:
                sent=desktop(TITLE,action+'\n'+link+'\nMute repeats: python3 workflow alert-ack '+key)
                record['desktop_id']=sent.get('notification_id')
                record['last_desktop_sent']=utcnow().isoformat()
                item['desktop_sent']=sent['notification_sent']
            phone=phone_config() if config['alerts']['phone_enabled'] else None
            if phone and phone.get('enabled') and not record.get('phone_receipt'):
                sent=api_post('messages.json',{'user':phone['user'],'title':TITLE,
                    'message':action,'url':link,'url_title':'Open job','priority':2,
                    'retry':300,'expire':1800})
                if sent.get('ok') and sent.get('receipt'):
                    record['phone_receipt']=sent['receipt']
                item['phone_queued']=bool(sent.get('ok'))
            state[key]=record;outcomes.append(item)
        _save(_StateIO(),state)
    return outcomes


class _StateIO:
    @staticmethod
    def read_json(path,default):
        try:return json.loads(path.read_text())
        except (FileNotFoundError,ValueError):return default

    @staticmethod
    def write_json(path,data):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(data,indent=2)+'\n')
