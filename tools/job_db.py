"""Canonical SQLite state, conservative legacy migration, and guarded transitions."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import uuid
from urllib.parse import urlsplit

from local_paths import ROOT

STATE=ROOT/'.workflow'
DB=STATE/'jobs.sqlite3'
MARKER=STATE/'canonical-db-enabled.json'
TERMINAL={'Rejected','Closed','Expired','Withdrawn','Excluded'}
ACTIVE={'Discovered','Verified','Shortlisted','ResumeReady','InProgress','Preparing','Prepared','NeedsAnswer','SubmissionUnconfirmed','Applied','Interview','Offer','NeedsReview'}
TRANSITIONS={
 'Discovered':{'Verified','Closed','Expired','Excluded'},
 'Verified':{'Shortlisted','InProgress','Excluded','Closed','Expired'},
 'InProgress':{'Preparing','Prepared','NeedsAnswer','SubmissionUnconfirmed','Closed','Expired'},
 'Shortlisted':{'Verified','ResumeReady','NeedsAnswer','Closed','Expired','Excluded'},
 'ResumeReady':{'InProgress','Preparing','NeedsAnswer','Closed','Expired'},
 'Preparing':{'Prepared','NeedsAnswer','Closed','Expired'},
 'Prepared':{'SubmissionUnconfirmed','NeedsAnswer','Closed','Expired'},
 'NeedsAnswer':{'Shortlisted','ResumeReady','InProgress','Preparing','NeedsReview','Closed','Expired'},
 'SubmissionUnconfirmed':{'Applied','NeedsReview'},
 'NeedsReview':{'Applied','NeedsAnswer','Preparing','Closed'},
 'Applied':{'Interview','Offer','Rejected','Withdrawn'},
 'Interview':{'Offer','Rejected','Withdrawn'},
 'Offer':{'Withdrawn'},
}

def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def norm(value):return re.sub(r'[^a-z0-9]','',str(value or '').lower())
def employer(value):
    key=norm(value)
    return 'unitedhealthgroup' if 'optum' in key or 'unitedhealthgroup' in key else key
def identity(company,req,url=None,role=None):
    company_key=employer(company)
    if not company_key:raise ValueError('Employer required')
    if req:return company_key,'req:'+norm(req)
    fallback=url or role
    if not fallback:raise ValueError('Requisition or original URL required')
    return company_key,'fallback:'+hashlib.sha256(str(fallback).encode()).hexdigest()
def data_json(value):return json.dumps(value,ensure_ascii=False,default=str,sort_keys=True)

def connect(path=DB):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=30,isolation_level=None)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=30000')
    db.execute('PRAGMA journal_mode=WAL')
    return db

SCHEMA='''
CREATE TABLE IF NOT EXISTS jobs(
 id INTEGER PRIMARY KEY,employer_key TEXT NOT NULL,requisition_key TEXT NOT NULL,
 employer TEXT NOT NULL,title TEXT NOT NULL,requisition_id TEXT,canonical_url TEXT,slug TEXT UNIQUE,
 source TEXT,location TEXT,discovered_at TEXT,posted_at TEXT,checked_at TEXT,
 jd_text TEXT,availability TEXT NOT NULL DEFAULT 'Unknown',provenance_json TEXT NOT NULL DEFAULT '{}',
 fit TEXT NOT NULL DEFAULT 'Unrated',fit_json TEXT NOT NULL DEFAULT '{}',
 state TEXT NOT NULL,status_date TEXT,known_reason TEXT,
 tracker_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 UNIQUE(employer_key,requisition_key));
CREATE UNIQUE INDEX IF NOT EXISTS unique_original_url ON jobs(canonical_url) WHERE canonical_url IS NOT NULL AND canonical_url!='';
CREATE TABLE IF NOT EXISTS history_keys(
 employer_key TEXT NOT NULL,requisition_key TEXT NOT NULL,company TEXT NOT NULL,role TEXT NOT NULL,
 requisition_id TEXT,direct_link TEXT,terminal_status TEXT,status_date TEXT,known_reason TEXT,
 source TEXT NOT NULL,PRIMARY KEY(employer_key,requisition_key));
CREATE TABLE IF NOT EXISTS evidence(
 id INTEGER PRIMARY KEY,job_id INTEGER NOT NULL REFERENCES jobs(id),kind TEXT NOT NULL,
 path TEXT NOT NULL,sha256 TEXT NOT NULL,captured_at TEXT,attempt_id TEXT,
 UNIQUE(job_id,path));
CREATE TABLE IF NOT EXISTS submission_attempts(
 attempt_id TEXT PRIMARY KEY,job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
 requisition_key TEXT NOT NULL,resume_path TEXT NOT NULL,resume_sha256 TEXT NOT NULL,
 form_url TEXT NOT NULL,form_evidence_path TEXT NOT NULL,form_sha256 TEXT NOT NULL,state TEXT NOT NULL,
 claimed_at TEXT NOT NULL,proof_path TEXT,proof_sha256 TEXT,proof_captured_at TEXT);
CREATE TABLE IF NOT EXISTS state_events(
 id INTEGER PRIMARY KEY,job_id INTEGER NOT NULL REFERENCES jobs(id),at TEXT NOT NULL,
 from_state TEXT,to_state TEXT NOT NULL,actor TEXT NOT NULL,reason TEXT,evidence_path TEXT);
CREATE TABLE IF NOT EXISTS legacy_artifacts(
 path TEXT PRIMARY KEY,sha256 TEXT NOT NULL,kind TEXT NOT NULL,job_id INTEGER REFERENCES jobs(id));
CREATE TABLE IF NOT EXISTS queue(
 job_id INTEGER PRIMARY KEY REFERENCES jobs(id),priority REAL NOT NULL,queued_at TEXT NOT NULL,
 state TEXT NOT NULL,worker_id TEXT,stage_started TEXT,stage_seconds_json TEXT NOT NULL DEFAULT '{}',
 retry_after TEXT,attempts INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS queue_rank ON queue(state,priority DESC,queued_at);
CREATE TABLE IF NOT EXISTS review_queue(
 job_id INTEGER PRIMARY KEY REFERENCES jobs(id),priority REAL NOT NULL,queued_at TEXT NOT NULL,
 state TEXT NOT NULL,reviewed_at TEXT,review_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS review_queue_rank ON review_queue(state,priority DESC,queued_at);
CREATE TABLE IF NOT EXISTS lead_queue(
 canonical_url TEXT PRIMARY KEY,source TEXT NOT NULL,title TEXT NOT NULL,
 first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'NeedsOriginal');
CREATE TABLE IF NOT EXISTS lead_provenance(
 canonical_url TEXT NOT NULL,source TEXT NOT NULL,portal_job_id TEXT NOT NULL,
 search_page_url TEXT NOT NULL,method TEXT NOT NULL,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,
 PRIMARY KEY(canonical_url,source,portal_job_id));
CREATE TABLE IF NOT EXISTS portal_card_leads(
 source TEXT NOT NULL,portal_job_id TEXT NOT NULL,search_page_url TEXT NOT NULL,
 canonical_url TEXT,title TEXT,status TEXT NOT NULL,failure_reason TEXT,
 first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,
 PRIMARY KEY(source,portal_job_id));
CREATE TABLE IF NOT EXISTS lead_checks(
 canonical_url TEXT PRIMARY KEY,status TEXT NOT NULL,checked_at TEXT NOT NULL,
 result_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS ats_cooldowns(host TEXT PRIMARY KEY,until TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portal_capability(
 portal TEXT PRIMARY KEY,capability TEXT NOT NULL,reason TEXT NOT NULL,session TEXT,checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT,mode TEXT NOT NULL,status TEXT NOT NULL,
 result_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS source_coverage(
 run_id TEXT NOT NULL REFERENCES runs(id),source TEXT NOT NULL,page_key TEXT NOT NULL,
 checked_at TEXT NOT NULL,status TEXT NOT NULL,lead_count INTEGER NOT NULL DEFAULT 0,
 verified_count INTEGER NOT NULL DEFAULT 0,error_kind TEXT,evidence_path TEXT,
 PRIMARY KEY(run_id,source,page_key));
'''

def initialize(db):
    db.executescript(SCHEMA)
    if 'trigger' not in {r[1] for r in db.execute('PRAGMA table_info(runs)')}:
        db.execute('ALTER TABLE runs ADD COLUMN trigger TEXT')
def canonical_enabled():return MARKER.is_file() and DB.is_file()

def legacy_state(value):
    value=str(value or '').strip();low=value.lower()
    if value in ACTIVE|TERMINAL:return value
    if low.startswith('excluded'):
        return 'Closed' if 'closed' in low else 'Excluded'
    if low in ('blocked','needs answer'):return 'NeedsAnswer'
    if low in ('draft saved','ready to submit'):return 'Prepared'
    if low=='submission unconfirmed':return 'SubmissionUnconfirmed'
    if low=='stopped':return 'NeedsReview'
    if low=='applied':return 'Applied'
    if low=='shortlisted':return 'Shortlisted'
    return 'Discovered'

def _insert_legacy_row(db,row):
    company=str(row.get('Company') or '').strip();role=str(row.get('Role') or '').strip()
    req=row.get('Job ID');url=row.get('Direct link');key=identity(company,req,url,role)
    state=legacy_state(row.get('Status'))
    stamp=now();tracker=data_json(row)
    db.execute('''INSERT INTO jobs(employer_key,requisition_key,employer,title,requisition_id,canonical_url,
      source,location,posted_at,checked_at,fit,state,status_date,known_reason,tracker_json,created_at,updated_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
      (key[0],key[1],company,role,str(req) if req is not None else None,url,row.get('Source'),row.get('Location'),
       str(row.get('Posted date')) if row.get('Posted date') else None,str(row.get('Verified date')) if row.get('Verified date') else None,
       row.get('Fit') or 'Unrated',state,str(row.get('Status date')) if row.get('Status date') else None,
       row.get('Known reason') or ('reason unknown' if state in TERMINAL else None),tracker,stamp,stamp))
    job_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
    db.execute('INSERT INTO state_events(job_id,at,to_state,actor,reason,evidence_path) VALUES(?,?,?,?,?,?)',
               (job_id,stamp,state,'migration','Exact legacy status: '+str(row.get('Status')),'tracker.xlsx'))
    if state in TERMINAL:
        db.execute('''INSERT INTO history_keys VALUES(?,?,?,?,?,?,?,?,?,?)''',
                   (key[0],key[1],company,role,str(req) if req is not None else None,url,state,
                    str(row.get('Status date')) if row.get('Status date') else None,row.get('Known reason') or 'reason unknown','tracker.xlsx'))
    return job_id,key

def migrate(root=ROOT):
    root=Path(root);state=root/'.workflow';database=state/'jobs.sqlite3';marker=state/'canonical-db-enabled.json'
    if marker.exists():return json.loads((state/'migration/verification.json').read_text())
    import sys
    sys.path.insert(0,str(root/'.local-deps'))
    from openpyxl import load_workbook
    import hashlib
    if not (root/'tracker.xlsx').is_file():raise ValueError('Legacy tracker missing')
    migration=state/'migration';migration.mkdir(parents=True,exist_ok=True)
    backup=migration/'tracker-before-sqlite.xlsx';shutil.copy2(root/'tracker.xlsx',backup)
    source_hash=hashlib.sha256(backup.read_bytes()).hexdigest()
    wb=load_workbook(backup,read_only=True,data_only=True);sheet=wb['Tracker']
    columns=[cell.value for cell in sheet[1]]
    rows=[dict(zip(columns,values)) for values in sheet.iter_rows(min_row=2,values_only=True)]
    temp=migration/'jobs-building.sqlite3'
    if temp.exists():temp.unlink()
    db=connect(temp);initialize(db)
    missing=[];row_keys=[];linked=0;artifacts=0
    try:
        db.execute('BEGIN IMMEDIATE')
        for row in rows:
            job_id,key=_insert_legacy_row(db,row);row_keys.append(key)
            path=row.get('Confirmation evidence')
            if path:
                artifact=(root/str(path)).resolve()
                if artifact.is_relative_to(root) and artifact.is_file():
                    digest=hashlib.sha256(artifact.read_bytes()).hexdigest()
                    db.execute('INSERT OR IGNORE INTO evidence(job_id,kind,path,sha256) VALUES(?,?,?,?)',
                               (job_id,'legacy_confirmation',str(artifact.relative_to(root)),digest));linked+=1
                else:missing.append(str(path))
        other=state/'cleanup/other_history_keys.json'
        if other.is_file():
            for record in json.loads(other.read_text()):
                company=record.get('Company');role=record.get('Role');req=record.get('Job ID');url=record.get('Direct link')
                key=identity(company,req,url,role)
                db.execute('''INSERT OR IGNORE INTO history_keys VALUES(?,?,?,?,?,?,?,?,?,?)''',
                           (key[0],key[1],company,role,req,url,record.get('Status') or 'Historical',
                            record.get('Status date'),record.get('Known reason') or 'reason unknown','other_history_keys.json'))
        # Every original file remains in place; register its content hash so history/evidence is auditable.
        candidates=[p for folder in ('jobs','captures') for p in (root/folder).rglob('*') if p.is_file()]
        candidates += [p for folder in ('changes','cleanup') for p in (state/folder).rglob('*') if p.is_file()]
        candidates += [p for p in (state/'events.jsonl',state/'historical-preview.json',backup) if p.is_file()]
        for file in candidates:
            rel=str(file.relative_to(root));digest=hashlib.sha256(file.read_bytes()).hexdigest()
            kind='job_file' if rel.startswith('jobs/') else 'capture' if rel.startswith('captures/') else 'audit'
            job_match=None
            if rel.startswith('jobs/'):
                slug=Path(rel).parts[1]
                listing=root/'jobs'/slug/'listing.json'
                record=json.loads(listing.read_text()) if listing.is_file() else {}
                if not record:
                    match=re.search(r'(?<!\d)(2388999)(?!\d)',slug)
                    if match:record={'company':'Optum','job_id':match.group(1)}
                if record:
                    try:
                        key=identity(record.get('company'),record.get('job_id'),record.get('url'),record.get('role'))
                        found=db.execute('SELECT id FROM jobs WHERE employer_key=? AND requisition_key=?',key).fetchone()
                        if found:
                            job_match=found['id']
                            db.execute('UPDATE jobs SET slug=coalesce(slug,?) WHERE id=?',(slug,job_match))
                    except ValueError:pass
            db.execute('INSERT OR IGNORE INTO legacy_artifacts(path,sha256,kind,job_id) VALUES(?,?,?,?)',
                       (rel,digest,kind,job_match));artifacts+=1
        db.commit()
    except Exception:
        db.rollback();db.close();raise
    # Verify before flipping the canonical marker.
    imported=db.execute('SELECT count(*) FROM jobs').fetchone()[0]
    if imported!=len(rows) or len(set(row_keys))!=len(rows):
        db.close();raise ValueError('Migration row count or dedup keys do not match workbook')
    optum=db.execute("SELECT state,known_reason,requisition_id FROM jobs WHERE employer_key='unitedhealthgroup' AND requisition_key='req:2388999'").fetchone()
    if not optum or optum['state']!='Rejected' or optum['known_reason']!='reason unknown':
        db.close();raise ValueError('Optum terminal status/reason did not survive migration')
    history_count=db.execute('SELECT count(*) FROM history_keys').fetchone()[0]
    if history_count<4:db.close();raise ValueError('Terminal/historical dedup keys missing')
    optum_files=db.execute("SELECT count(*) FROM legacy_artifacts WHERE job_id=(SELECT id FROM jobs WHERE employer_key='unitedhealthgroup' AND requisition_key='req:2388999')").fetchone()[0]
    check=db.execute('PRAGMA integrity_check').fetchone()[0]
    if check!='ok':db.close();raise ValueError('SQLite integrity check failed: '+check)
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    db.execute('PRAGMA journal_mode=DELETE')
    db.close();temp.replace(database)
    report={'migrated_at':now(),'source':'tracker.xlsx','backup':str(backup.relative_to(root)),
            'source_sha256':source_hash,'columns':columns,'legacy_rows':len(rows),'sqlite_rows':imported,
            'dedup_keys':history_count,'linked_confirmation_evidence':linked,'registered_legacy_artifacts':artifacts,
            'missing_legacy_confirmation_paths':missing,'optum_2388999':dict(optum),
            'optum_registered_artifacts':optum_files,
            'unique_identity_keys':len(set(row_keys)),'integrity_check':check,'submissions_enabled':False}
    (migration/'verification.json').write_text(json.dumps(report,indent=2,default=str)+'\n')
    marker.write_text(json.dumps({'enabled_at':now(),'database':str(database.relative_to(root)),
                                  'verification_report':str((migration/'verification.json').relative_to(root))},indent=2)+'\n')
    return report

def rows(db=None):
    own=db is None;db=db or connect()
    try:return [dict(r) for r in db.execute('SELECT * FROM jobs ORDER BY id')]
    finally:
        if own:db.close()

def tracker_rows(db=None):
    result=[]
    for record in rows(db):
        legacy=json.loads(record['tracker_json'])
        legacy.update({'Company':record['employer'],'Role':record['title'],'Job ID':record['requisition_id'],
                       'Direct link':record['canonical_url'],'Source':record['source'],'Location':record['location'],
                       'Posted date':record['posted_at'],'Verified date':record['checked_at'],'Fit':record['fit'],
                       'Status':record['state'],'Status date':record['status_date'],'Known reason':record['known_reason']})
        result.append(legacy)
    return result

def require_writable_action(test_mode=False,config=None):
    from job_config import load
    config=config or load()
    if test_mode or os.environ.get('JOB_SEARCH_TEST_MODE')=='1' or config['mode']=='read_only' or not config['submissions']['enabled']:
        raise ValueError('Application mutation blocked by enforced read-only/test mode')
    if not (STATE/'full-application-path-verified.json').is_file():
        raise ValueError('Full application path has not been verified')

def naukri_worker_path_verified(db):
    """Require a still-valid, job-bound proof of the direct Naukri path."""
    marker_path=STATE/'full-application-path-verified.json'
    try:marker=json.loads(marker_path.read_text())
    except (OSError,ValueError,TypeError):return False
    if marker.get('status')!='verified' or marker.get('provider')!='naukri_portal_native':return False
    if marker.get('independently_reconciled') is False:return False
    attempt_id=marker.get('attempt_id')
    row=db.execute('''SELECT a.*,j.id AS exact_job_id,j.requisition_id,j.source,j.state AS job_state,j.slug
        FROM submission_attempts a JOIN jobs j ON j.id=a.job_id WHERE a.attempt_id=?''',(attempt_id,)).fetchone()
    # Avoid accepting a marker merely because a path exists: the attempt and
    # proof must still agree with the canonical database and on-disk evidence.
    if not row or row['job_state'] not in ('Applied','Interview','Offer','Rejected','Withdrawn') or row['source']!='naukri_portal_native' or row['state']!='confirmed':return False
    if marker.get('job_id')!=row['exact_job_id'] or str(marker.get('requisition_id'))!=str(row['requisition_id']):return False
    if row['attempt_id'] != attempt_id or row['proof_path'] != marker.get('proof_path'):
        return False
    proof_path=(ROOT/row['proof_path']).resolve()
    if not proof_path.is_relative_to((ROOT/'jobs'/row['slug']).resolve()) or not proof_path.is_file():return False
    digest=hashlib.sha256(proof_path.read_bytes()).hexdigest()
    if digest!=marker.get('proof_sha256'):return False
    try:proof=json.loads(proof_path.read_text())
    except (OSError,ValueError,TypeError):return False
    if (proof.get('attempt_id')!=attempt_id or str(proof.get('requisition_id'))!=str(row['requisition_id'])
        or not (proof.get('history_full') is True and proof.get('exact_history_id_match') is True)):return False
    outcome_path=proof_path.parent/'naukri-native-outcome.json'
    if not outcome_path.is_file():outcome_path=proof_path.parent/'naukri-native-attempt-outcome.json'
    try:outcome=json.loads(outcome_path.read_text())
    except (OSError,ValueError,TypeError):return False
    return outcome.get('generic_resume_restored') is True or outcome.get('generic_restored_verified') is True

def portal_path_verified(db,portal):
    """Per-portal proof that a real application reached a confirmed, job-bound Applied state."""
    if portal=='naukri':return naukri_worker_path_verified(db)
    try:marker=json.loads((STATE/'portal-proof'/f'{portal}.json').read_text())
    except (OSError,ValueError,TypeError):return False
    if marker.get('status')!='verified' or marker.get('provider')!=portal:return False
    if marker.get('candidate_facts_reconciled') is False:
        # A user may authorize an existing confirmation as technical-path proof
        # without turning a job-specific answer override into candidate facts.
        if portal!='linkedin' or marker.get('technical_path_authorized_by_user') is not True:return False
        if marker.get('experience_override_reusable') is not False:return False
        if not str(marker.get('requisition_id') or '').isdigit():return False
        audit_path=ROOT/'jobs'/('linkedin-'+str(marker['requisition_id']))/'application-outcome-audit.json'
        private_path=STATE/'private-candidate-confirmations'/('linkedin-'+str(marker['requisition_id'])+'.json')
        try:
            audit=json.loads(audit_path.read_text());private=json.loads(private_path.read_text())
        except (OSError,ValueError,TypeError):return False
        if (audit.get('experience_answer_one_off_user_override') is not True
                or audit.get('override_reusable') is not False
                or private.get('experience_answers_valid') is not False
                or private.get('experience_override_reusable') is not False):return False
    row=db.execute("""SELECT a.state,a.proof_path,a.proof_sha256,a.resume_sha256,j.id,j.requisition_id,j.source,j.state AS job_state,j.slug FROM submission_attempts a
        JOIN jobs j ON j.id=a.job_id WHERE a.attempt_id=?""",(marker.get('attempt_id'),)).fetchone()
    if not row or row['job_state'] not in ('Applied','Interview','Offer','Rejected','Withdrawn') or row['state']!='confirmed' or row['proof_path']!=marker.get('proof_path'):return False
    if row['source']!=portal+'_portal_native' or marker.get('job_id')!=row['id'] or str(marker.get('requisition_id'))!=str(row['requisition_id']):return False
    proof=(ROOT/row['proof_path']).resolve()
    if not proof.is_relative_to((ROOT/'jobs'/row['slug']).resolve()) or not proof.is_file():return False
    if hashlib.sha256(proof.read_bytes()).hexdigest()!=marker.get('proof_sha256') or row['proof_sha256']!=marker.get('proof_sha256'):return False
    try:p=json.loads(proof.read_text())
    except (OSError,ValueError):return False
    return (p.get('attempt_id')==marker.get('attempt_id') and str(p.get('requisition_id'))==str(row['requisition_id'])
        and p.get('application_sent') is True and p.get('exact_job_id_match') is True
        and p.get('role_resume_sha256')==row['resume_sha256']
        and urlsplit(str(p.get('url') or '')).path.rstrip('/')=='/jobs/view/'+str(row['requisition_id']))

def _safe_incomplete_evidence(job,attempt,evidence_path):
    """Read-only proof that a claimed questionnaire stopped BEFORE any completion and is not applied."""
    path=(ROOT/str(evidence_path)).resolve()
    if not path.is_relative_to((ROOT/'jobs'/str(job['slug'])).resolve()) or not path.is_file():
        raise ValueError('Incomplete-questionnaire evidence must be saved in the exact job folder')
    proof=json.loads(path.read_text())
    if (proof.get('attempt_id')!=attempt['attempt_id'] or str(proof.get('requisition_id'))!=str(job['requisition_id'])
            or proof.get('safe') is not True or proof.get('completion_count')!=0 or proof.get('workflow_committed') is not False
            or proof.get('denied_events')!=0 or proof.get('save_in_flight') is not False
            or proof.get('listing_applied') is not False or proof.get('history_full') is not True
            or proof.get('history_exact')!=0 or not proof.get('pending_question')):
        raise ValueError('Evidence does not prove the questionnaire is safely incomplete')
    if dt.datetime.fromisoformat(proof['checked_at'])<dt.datetime.fromisoformat(attempt['claimed_at']):
        raise ValueError('Evidence predates the claimed attempt')
    return path

def mark_attempt_incomplete(db,attempt_id,job_id,evidence_path):
    """Claimed attempt -> safely incomplete (job NeedsAnswer). Never used for an ambiguous Save; never sets Applied."""
    db.execute('BEGIN IMMEDIATE')
    try:
        attempt=db.execute('SELECT * FROM submission_attempts WHERE attempt_id=? AND job_id=?',(attempt_id,job_id)).fetchone()
        job=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not attempt or not job or attempt['state']!='uncertain' or job['state'] not in ('SubmissionUnconfirmed','InProgress'):
            raise ValueError('Only an in-flight claimed attempt can be marked incomplete')
        if job['requisition_key']!=attempt['requisition_key']:raise ValueError('Job/requisition no longer matches attempt')
        _safe_incomplete_evidence(job,attempt,evidence_path)
        stamp=now()
        db.execute("UPDATE submission_attempts SET state='incomplete' WHERE attempt_id=?",(attempt_id,))
        db.execute("UPDATE jobs SET state='NeedsAnswer',status_date=?,updated_at=? WHERE id=?",(stamp,stamp,job_id))
        db.execute('INSERT INTO state_events(job_id,at,from_state,to_state,actor,reason,evidence_path) VALUES(?,?,?,?,?,?,?)',
                   (job_id,stamp,job['state'],'NeedsAnswer','incomplete_gate','Claimed questionnaire stopped before completion; answer needed',evidence_path))
        db.commit()
    except Exception:db.rollback();raise

def resume_incomplete_attempt(db,attempt_id,job_id):
    """Continue the SAME claimed attempt (no new claim): incomplete -> in-flight again (job InProgress)."""
    db.execute('BEGIN IMMEDIATE')
    try:
        attempt=db.execute('SELECT * FROM submission_attempts WHERE attempt_id=? AND job_id=?',(attempt_id,job_id)).fetchone()
        job=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not attempt or not job or attempt['state']!='incomplete' or job['state']!='NeedsAnswer':
            raise ValueError('Only a safely incomplete attempt can be continued')
        history=db.execute('SELECT 1 FROM history_keys WHERE employer_key=? AND requisition_key=?',(job['employer_key'],job['requisition_key'])).fetchone()
        if history:raise ValueError('Historical application key blocks continuation')
        stamp=now()
        db.execute("UPDATE submission_attempts SET state='uncertain' WHERE attempt_id=?",(attempt_id,))
        db.execute("UPDATE jobs SET state='InProgress',status_date=?,updated_at=? WHERE id=?",(stamp,stamp,job_id))
        db.execute('INSERT INTO state_events(job_id,at,from_state,to_state,actor,reason) VALUES(?,?,?,?,?,?)',
                   (job_id,stamp,'NeedsAnswer','InProgress','incomplete_gate','Continuing the same claimed attempt with a confirmed answer'))
        db.commit()
    except Exception:db.rollback();raise

def resume_authorized_uncertain_attempt(db,attempt_id,job_id,authorization_path):
    """One explicitly authorized same-attempt continuation; never a new claim or a general retry switch."""
    import job_config,control
    if control.is_paused() or not job_config.live_naukri_enabled(job_config.load()):raise ValueError('Live continuation is disabled')
    db.execute('BEGIN IMMEDIATE')
    try:
        row=db.execute('SELECT a.*,j.state job_state,j.requisition_id FROM submission_attempts a JOIN jobs j ON j.id=a.job_id WHERE a.attempt_id=? AND j.id=?',(attempt_id,job_id)).fetchone()
        if not row or row['state']!='uncertain' or row['job_state']!='SubmissionUnconfirmed':raise ValueError('Scoped uncertain attempt is not eligible')
        if str(row['requisition_id']) in {str(x) for x in job_config.load()['naukri_worker']['excluded_requisition_ids']}:raise ValueError('Protected requisition cannot continue')
        path=(ROOT/authorization_path).resolve()
        if not path.is_relative_to(STATE/'authorized-continuations'):raise ValueError('Continuation authorization path is invalid')
        auth=json.loads(path.read_text())
        if (auth.get('approved_by')!='user' or auth.get('job_id')!=job_id or auth.get('attempt_id')!=attempt_id
                or str(auth.get('requisition_id'))!=str(row['requisition_id']) or auth.get('max_continuations')!=1
                or auth.get('used_at') or auth.get('resume_sha256')!=row['resume_sha256']):
            raise ValueError('Exact unused user authorization is required')
        if db.execute("SELECT 1 FROM state_events WHERE job_id=? AND actor='authorized-continuation' AND reason=?",(job_id,attempt_id)).fetchone():raise ValueError('This continuation was already consumed')
        stamp=now();db.execute("UPDATE jobs SET state='InProgress',status_date=?,updated_at=? WHERE id=?",(stamp,stamp,job_id))
        db.execute('INSERT INTO state_events(job_id,at,from_state,to_state,actor,reason,evidence_path) VALUES(?,?,?,?,?,?,?)',
                   (job_id,stamp,'SubmissionUnconfirmed','InProgress','authorized-continuation',attempt_id,authorization_path))
        db.commit();auth['used_at']=stamp;path.write_text(json.dumps(auth,indent=2)+'\n')
    except Exception:db.rollback();raise

def record_observed_naukri_application(db,job_id,evidence_path):
    """Import positive exact portal evidence without inventing a workflow submission attempt."""
    db.execute('BEGIN IMMEDIATE')
    try:
        job=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not job or job['source']!='naukri_portal_native' or job['state'] not in ('NeedsAnswer','Verified','Shortlisted','ResumeReady'):
            raise ValueError('Only an unclaimed Naukri record can reconcile a previously observed application')
        if db.execute('SELECT 1 FROM submission_attempts WHERE job_id=?',(job_id,)).fetchone():
            raise ValueError('Claimed attempts must use their own confirmation gate')
        artifact=(ROOT/evidence_path).resolve()
        if not artifact.is_relative_to((ROOT/'jobs'/job['slug']).resolve()) or not artifact.is_file():
            raise ValueError('Observation proof must belong to the exact job')
        proof=json.loads(artifact.read_text())
        history=proof.get('history_identity',{})
        full_history=(proof.get('history_full') is True and proof.get('exact_history_id_match') is True
                      and history.get('basis')=='server_history_exact_job_id'
                      and any(str(r.get('job_id'))==str(job['requisition_id'])
                              and any(s.get('statusValue') in ('Applied','Application Sent') for s in r.get('status',[]))
                              for r in history.get('server_rows',[])))
        if (str(proof.get('requisition_id'))!=str(job['requisition_id']) or proof.get('url')!=job['canonical_url']
                or not (full_history or proof.get('exact_listing_applied') is True)
                or proof.get('workflow_worker_proven') is not False):
            raise ValueError('Positive exact listing or complete exact-ID server history and observed origin are required')
        if proof.get('exact_history_id_match') is True and not full_history:
            raise ValueError('Positive history reconciliation requires complete exact-ID server history')
        stamp=now();digest=hashlib.sha256(artifact.read_bytes()).hexdigest()
        db.execute('INSERT OR REPLACE INTO evidence(job_id,kind,path,sha256,captured_at) VALUES(?,?,?,?,?)',
            (job_id,'observed_application',evidence_path,digest,proof['captured_at']))
        db.execute("UPDATE jobs SET state='Applied',status_date=?,updated_at=? WHERE id=?",(stamp,stamp,job_id))
        db.execute('INSERT INTO state_events(job_id,at,from_state,to_state,actor,reason,evidence_path) VALUES(?,?,?,?,?,?,?)',
            (job_id,stamp,job['state'],'Applied','portal_observation','Previously applied: positive exact portal evidence observed; no workflow attempt invented',evidence_path))
        db.execute('INSERT OR REPLACE INTO history_keys VALUES(?,?,?,?,?,?,?,?,?,?)',
            (job['employer_key'],job['requisition_key'],job['employer'],job['title'],job['requisition_id'],job['canonical_url'],
             'Applied',stamp,None,'portal_observation'))
        db.execute('DELETE FROM queue WHERE job_id=?',(job_id,));db.commit()
    except Exception:db.rollback();raise

def transition(db,job_id,to_state,actor,reason,evidence_path=None,expected_state=None):
    if to_state not in ACTIVE|TERMINAL:raise ValueError('Unknown application state')
    db.execute('BEGIN IMMEDIATE')
    try:
        row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not row:raise ValueError('Unknown job')
        current=row['state']
        if expected_state and current!=expected_state:raise ValueError('State changed concurrently')
        if to_state not in TRANSITIONS.get(current,set()):raise ValueError(f'Forbidden state transition {current} -> {to_state}')
        if to_state in ('Applied','Rejected','Interview','Offer','Withdrawn'):
            if not evidence_path:raise ValueError('Proof required for this transition')
            if to_state=='Applied':
                attempt=db.execute('SELECT * FROM submission_attempts WHERE job_id=?',(job_id,)).fetchone()
                if not attempt or attempt['state']!='confirmed' or attempt['proof_path']!=evidence_path:
                    raise ValueError('Applied requires confirmed exact submission attempt')
            else:
                proof=db.execute("SELECT 1 FROM evidence WHERE job_id=? AND path=? AND kind='response'",(job_id,evidence_path)).fetchone()
                if not proof:raise ValueError('Later application state requires job-bound response evidence')
        stamp=now()
        db.execute('UPDATE jobs SET state=?,status_date=?,known_reason=?,updated_at=? WHERE id=?',
                   (to_state,stamp,row['known_reason'] or ('reason unknown' if to_state in TERMINAL else None),stamp,job_id))
        db.execute('INSERT INTO state_events(job_id,at,from_state,to_state,actor,reason,evidence_path) VALUES(?,?,?,?,?,?,?)',
                   (job_id,stamp,current,to_state,actor,reason,evidence_path))
        if to_state in TERMINAL:
            db.execute('''INSERT OR REPLACE INTO history_keys VALUES(?,?,?,?,?,?,?,?,?,?)''',
                       (row['employer_key'],row['requisition_key'],row['employer'],row['title'],row['requisition_id'],
                        row['canonical_url'],to_state,stamp,row['known_reason'] or 'reason unknown','state_machine'))
            db.execute("DELETE FROM queue WHERE job_id=?",(job_id,))
        elif to_state=='Applied':
            # A confirmed application is no longer runnable resume/application work.
            db.execute("DELETE FROM queue WHERE job_id=?",(job_id,))
        db.commit();return to_state
    except Exception:db.rollback();raise

def reopen_excluded_portal_for_reassessment(db,job_id,portal_job_id,employer_name,evidence_path,reason):
    """Reopen only this untouched Naukri exclusion after its evidence changes."""
    if str(portal_job_id)!='070926003879' or job_id!=244:
        raise ValueError('This audited reassessment is restricted to the specified untouched portal lead')
    if not evidence_path or not (ROOT/evidence_path).is_file():
        raise ValueError('Reassessment evidence must exist inside the project')
    db.execute('BEGIN IMMEDIATE')
    try:
        row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        exact_url='https://www.naukri.com/job-listings-java-backend-developer-naukri-e-hire-campaign-mumbai-1-to-4-years-070926003879'
        if (not row or row['source']!='naukri_portal_native' or row['state']!='Excluded' or row['canonical_url']!=exact_url):
            raise ValueError('Exact excluded Naukri requisition does not match')
        provenance=json.loads(row['provenance_json'] or '{}')
        if str(provenance.get('portal_job_id'))!=str(portal_job_id):raise ValueError('Portal ID evidence mismatch')
        if db.execute('SELECT 1 FROM submission_attempts WHERE job_id=?',(job_id,)).fetchone():
            raise ValueError('Existing application attempt blocks reassessment')
        old_key=(row['employer_key'],row['requisition_key'])
        old_history=db.execute('SELECT * FROM history_keys WHERE employer_key=? AND requisition_key=?',old_key).fetchone()
        if not old_history or old_history['terminal_status']!='Excluded':
            raise ValueError('Expected prior Excluded deduplication record is missing')
        employer_key,requisition_key=identity(employer_name,portal_job_id,row['canonical_url'],row['title'])
        other_history=db.execute('SELECT 1 FROM history_keys WHERE employer_key=? AND requisition_key=?',
                                 (employer_key,requisition_key)).fetchone()
        other_job=db.execute('SELECT id FROM jobs WHERE employer_key=? AND requisition_key=? AND id<>?',
                             (employer_key,requisition_key,job_id)).fetchone()
        if other_history or other_job:raise ValueError('Duplicate/history under verified employer identity blocks reassessment')
        stamp=now()
        db.execute('DELETE FROM history_keys WHERE employer_key=? AND requisition_key=?',old_key)
        db.execute('''UPDATE jobs SET employer=?,employer_key=?,requisition_id=?,requisition_key=?,
          state='Verified',known_reason=NULL,updated_at=? WHERE id=?''',
          (employer_name,employer_key,str(portal_job_id),requisition_key,stamp,job_id))
        db.execute('''INSERT INTO state_events(job_id,at,from_state,to_state,actor,reason,evidence_path)
          VALUES(?,?,?,?,?,?,?)''',(job_id,stamp,'Excluded','Verified','evidence_reassessment',reason,evidence_path))
        db.commit()
        return {'job_id':job_id,'from_state':'Excluded','to_state':'Verified','old_history_key_removed':True,'attempt_count':0}
    except Exception:
        db.rollback();raise

def create_job(db,record):
    required=('employer','title','canonical_url','source','discovered_at')
    if any(not record.get(k) for k in required):raise ValueError('Normalized job identity fields missing')
    key=identity(record['employer'],record.get('requisition_id'),record['canonical_url'],record['title'])
    db.execute('BEGIN IMMEDIATE')
    try:
        if db.execute('SELECT 1 FROM history_keys WHERE employer_key=? AND requisition_key=?',key).fetchone():
            raise ValueError('Historical requisition: never recreate or reapply')
        prior=db.execute('SELECT * FROM jobs WHERE employer_key=? AND requisition_key=?',key).fetchone()
        if prior:
            if prior['state'] in ('Discovered','Verified'):
                changed=bool(prior['jd_text'] and record.get('jd_text') and prior['jd_text']!=record['jd_text'])
                db.execute('''UPDATE jobs SET title=?,location=coalesce(?,location),posted_at=coalesce(?,posted_at),
                  checked_at=?,jd_text=?,availability=?,provenance_json=?,
                  fit=CASE WHEN ? THEN 'Unrated' ELSE fit END,
                  fit_json=CASE WHEN ? THEN '{}' ELSE fit_json END,updated_at=? WHERE id=?''',
                  (record['title'],record.get('location'),record.get('posted_at'),record.get('checked_at'),record.get('jd_text'),record.get('availability','Unknown'),
                   data_json(record.get('provenance',{})),changed,changed,now(),prior['id']))
                if changed:db.execute('DELETE FROM queue WHERE job_id=?',(prior['id'],))
            db.commit();return prior['id'],False
        stamp=now()
        slug=record.get('slug') or '-'.join(x for x in (norm(record['employer'])[:28],norm(record['title'])[:32],norm(record.get('requisition_id'))[:32]) if x)
        if not re.fullmatch(r'[a-z0-9-]+',slug):raise ValueError('Unsafe job slug')
        cur=db.execute('''INSERT INTO jobs(employer_key,requisition_key,employer,title,requisition_id,canonical_url,slug,
          source,location,discovered_at,posted_at,checked_at,jd_text,availability,provenance_json,
          state,tracker_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (key[0],key[1],record['employer'],record['title'],record.get('requisition_id'),record['canonical_url'],slug,
           record['source'],record.get('location'),record['discovered_at'],record.get('posted_at'),record.get('checked_at'),
           record.get('jd_text'),record.get('availability','Unknown'),data_json(record.get('provenance',{})),
           'Discovered','{}',stamp,stamp))
        db.execute('INSERT INTO state_events(job_id,at,to_state,actor,reason) VALUES(?,?,?,?,?)',
                   (cur.lastrowid,stamp,'Discovered','pipeline','Normalized source lead'))
        db.commit();return cur.lastrowid,True
    except Exception:db.rollback();raise

def bind_verified_portal_id(db,job_id):
    """Promote a verified Naukri job ID to the atomic deduplication key."""
    db.execute('BEGIN IMMEDIATE')
    try:
        row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not row or row['source'] not in ('naukri_portal_native','linkedin_portal_native') or row['state']!='Verified':
            raise ValueError('Only a verified, untouched Naukri job can bind its portal ID')
        provenance=json.loads(row['provenance_json'] or '{}')
        portal_id=str(provenance.get('portal_job_id') or '')
        if (provenance.get('verification_method')!='portal_native' or
                not re.fullmatch(r'\d{8,}',portal_id) or
                not (str(row['canonical_url'] or '').rstrip('/').endswith('-'+portal_id) or
                     row['source']=='linkedin_portal_native' and urlsplit(str(row['canonical_url'] or '')).path.rstrip('/')=='/jobs/view/'+portal_id) or
                provenance.get('duplicate_check',{}).get('status')!='clear'):
            raise ValueError('Verified portal identity or duplicate clearance missing')
        key=identity(row['employer'],portal_id,row['canonical_url'],row['title'])
        if row['requisition_id'] is not None:
            if row['requisition_id']==portal_id and row['requisition_key']==key[1]:
                db.commit();return portal_id
            raise ValueError('Job already has a different requisition identity')
        if db.execute('SELECT 1 FROM history_keys WHERE employer_key=? AND requisition_key=?',key).fetchone():
            raise ValueError('Portal requisition exists in terminal history')
        if db.execute('SELECT 1 FROM jobs WHERE employer_key=? AND requisition_key=? AND id<>?',(*key,job_id)).fetchone():
            raise ValueError('Portal requisition already belongs to another job')
        if db.execute('SELECT 1 FROM submission_attempts WHERE job_id=?',(job_id,)).fetchone():
            raise ValueError('Existing submission attempt blocks identity change')
        db.execute('UPDATE jobs SET requisition_id=?,requisition_key=?,updated_at=? WHERE id=?',
                   (portal_id,key[1],now(),job_id))
        db.commit();return portal_id
    except Exception:db.rollback();raise

def apply_fit_decision(db,job_id,decision,config):
    import fit_engine
    db.execute('BEGIN IMMEDIATE')
    try:
        job=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not job or job['state']!='Verified':raise ValueError('Only verified exact job JDs can receive a new fit decision')
        provenance=json.loads(job['provenance_json'] or '{}')
        if provenance.get('latest_live_jd_status')=='TitleOnly':
            raise ValueError('Latest exact job page lacks a substantive JD; fresh verification required')
        verdict=fit_engine.evaluate(dict(job),decision,config,ROOT/'master-resume/Yugal_Teotia_CV.tex')
        import review_candidates
        if verdict['fit'] in ('High','Medium') and (verdict['eligible'] or verdict.get('strong_medium_eligible')) and not job['requisition_id'] and not review_candidates.verified_portal_id(provenance):
            verdict={'fit':'Medium','eligible':False,'reasons':verdict['reasons']+['Employer requisition ID missing'],
                     'jd_sha256':verdict['jd_sha256'],'essential_total':verdict['essential_total'],
                     'essential_work_supported':verdict['essential_work_supported']}
        duplicate=provenance.get('duplicate_check',{})
        if verdict['fit'] in ('High','Medium') and (verdict['eligible'] or verdict.get('strong_medium_eligible')) and duplicate.get('status')=='uncertain':
            verdict={**verdict,'eligible':False,'submission_blocked_by_duplicate':True,
                     'strong_medium_eligible':False,
                     'reasons':verdict['reasons']+['Possible duplicate requisition requires manual resolution before any application']}
        worker_eligible=bool(verdict['eligible'] or
                             (job['source'] in ('naukri_portal_native','linkedin_portal_native') and verdict.get('strong_medium_eligible')))
        stamp=now();state='Shortlisted' if worker_eligible else 'Verified'
        db.execute('UPDATE jobs SET fit=?,fit_json=?,state=?,updated_at=? WHERE id=?',
                   (verdict['fit'],data_json({'decision':decision,'result':verdict}),state,stamp,job_id))
        if worker_eligible:
            if db.execute('SELECT 1 FROM history_keys WHERE employer_key=? AND requisition_key=?',
                          (job['employer_key'],job['requisition_key'])).fetchone():
                raise ValueError('Historical application blocks Naukri application queueing')
            if db.execute('SELECT 1 FROM submission_attempts WHERE job_id=?',(job_id,)).fetchone():
                raise ValueError('Uncertain application blocks queueing')
            age_hours=10000
            if job['posted_at']:
                try:
                    posted=dt.datetime.fromisoformat(job['posted_at'].replace('Z','+00:00'))
                    if posted.tzinfo is None:posted=posted.replace(tzinfo=dt.timezone.utc)
                    age_hours=max(0,(dt.datetime.now(dt.timezone.utc)-posted).total_seconds()/3600)
                except ValueError:pass
            priority=100+max(0,20-min(20,age_hours/12))
            db.execute("INSERT INTO queue(job_id,priority,queued_at,state) VALUES(?,?,?,'QueuedResume') ON CONFLICT(job_id) DO NOTHING",
                       (job_id,priority,stamp))
            db.execute('INSERT INTO state_events(job_id,at,from_state,to_state,actor,reason) VALUES(?,?,?,?,?,?)',
                       (job_id,stamp,'Verified','Shortlisted','fit_engine',
                        'Approved strong Medium with recorded transferable gaps' if verdict.get('strong_medium_eligible') else 'All essential JD excerpts and master work evidence passed'))
        db.commit();return verdict
    except Exception:db.rollback();raise

def refresh_staged_fit_decision(db,job_id,decision,config):
    """Replace a staged packet only after a fresh full evidence check; preserve its queue state."""
    import fit_engine
    db.execute('BEGIN IMMEDIATE')
    try:
        job=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not job or job['state'] not in ('Shortlisted','ResumeReady','Prepared'):
            raise ValueError('Only an untouched staged job can refresh its fit evidence')
        if db.execute('SELECT 1 FROM submission_attempts WHERE job_id=?',(job_id,)).fetchone():
            raise ValueError('A claimed attempt cannot refresh or retry')
        if db.execute('SELECT 1 FROM history_keys WHERE employer_key=? AND requisition_key=?',(job['employer_key'],job['requisition_key'])).fetchone():
            raise ValueError('Previously applied requisition cannot refresh')
        verdict=fit_engine.evaluate(dict(job),decision,config,ROOT/'master-resume/Yugal_Teotia_CV.tex')
        if not (verdict['eligible'] or verdict.get('strong_medium_eligible')):
            raise ValueError('Updated evidence does not pass the approved fit policy')
        db.execute('UPDATE jobs SET fit=?,fit_json=?,updated_at=? WHERE id=?',
                   (verdict['fit'],data_json({'decision':decision,'result':verdict}),now(),job_id))
        db.commit();return verdict
    except Exception:db.rollback();raise

def claim_queue(db,stage,worker_id,config,*,test_mode=False):
    if stage not in ('resume','application'):raise ValueError('Invalid queue stage')
    if stage=='application':require_writable_action(test_mode,config)
    if not worker_id or len(worker_id)>100:raise ValueError('Invalid worker ID')
    source='QueuedResume' if stage=='resume' else 'ResumeReady'
    target='Resuming' if stage=='resume' else 'Applying'
    db.execute('BEGIN IMMEDIATE')
    try:
        candidates=db.execute('''SELECT q.*,j.employer_key,j.canonical_url,j.requisition_key,j.state AS job_state
          FROM queue q JOIN jobs j ON j.id=q.job_id WHERE q.state=? ORDER BY q.priority DESC,q.queued_at,q.job_id LIMIT 50''',(source,)).fetchall()
        for row in candidates:
            if row['job_state']!=('Shortlisted' if stage=='resume' else 'ResumeReady'):continue
            host=urlsplit(row['canonical_url'] or '').hostname or ''
            if db.execute('SELECT 1 FROM ats_cooldowns WHERE host=? AND until>?',(host,now())).fetchone():continue
            active=db.execute('''SELECT 1 FROM queue q JOIN jobs j ON j.id=q.job_id
              WHERE q.state IN ('Resuming','Applying','Submitting') AND
              (j.employer_key=? OR j.canonical_url LIKE ?) LIMIT 1''',
              (row['employer_key'],'https://'+host+'/%')).fetchone()
            if active:continue
            stamp=now();db.execute('UPDATE queue SET state=?,worker_id=?,stage_started=?,attempts=attempts+1 WHERE job_id=?',
                                   (target,worker_id,stamp,row['job_id']))
            db.commit();return {'job_id':row['job_id'],'worker_id':worker_id,'stage':stage,'priority':row['priority']}
        db.commit();return None
    except Exception:db.rollback();raise

def queue_finish_resume(db,job_id,worker_id,resume_review_path):
    db.execute('BEGIN IMMEDIATE')
    try:
        row=db.execute('SELECT q.*,j.state AS job_state FROM queue q JOIN jobs j ON j.id=q.job_id WHERE q.job_id=?',(job_id,)).fetchone()
        if not row or row['state']!='Resuming' or row['worker_id']!=worker_id or row['job_state']!='Shortlisted':
            raise ValueError('Resume completion does not match active worker/job state')
        if not db.execute("SELECT 1 FROM evidence WHERE job_id=? AND kind='resume_review' AND path=?",(job_id,resume_review_path)).fetchone():
            raise ValueError('Job-bound visual resume evidence required')
        elapsed=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(row['stage_started'])).total_seconds()
        times=json.loads(row['stage_seconds_json']);times['resume']=round(elapsed,3);stamp=now()
        db.execute("UPDATE queue SET state='ResumeReady',worker_id=NULL,stage_started=NULL,stage_seconds_json=? WHERE job_id=?",
                   (data_json(times),job_id))
        db.execute("UPDATE jobs SET state='ResumeReady',updated_at=? WHERE id=?",(stamp,job_id))
        db.execute('INSERT INTO state_events(job_id,at,from_state,to_state,actor,reason,evidence_path) VALUES(?,?,?,?,?,?,?)',
                   (job_id,stamp,'Shortlisted','ResumeReady',worker_id,'Visual review bound to exact PDF',resume_review_path))
        db.commit()
    except Exception:db.rollback();raise

def queue_block(db,job_id,worker_id,reason,evidence_path):
    if not reason or not evidence_path:raise ValueError('Blocker requires reason and saved evidence')
    db.execute('BEGIN IMMEDIATE')
    try:
        row=db.execute('SELECT q.*,j.state AS job_state FROM queue q JOIN jobs j ON j.id=q.job_id WHERE q.job_id=?',(job_id,)).fetchone()
        if not row or row['worker_id']!=worker_id or row['state'] not in ('Resuming','Applying'):
            raise ValueError('Only owning active worker may block a job')
        if 'NeedsAnswer' not in TRANSITIONS.get(row['job_state'],set()):raise ValueError('Job cannot enter NeedsAnswer')
        stamp=now();db.execute("UPDATE queue SET state='Blocked',worker_id=NULL,stage_started=NULL WHERE job_id=?",(job_id,))
        db.execute("UPDATE jobs SET state='NeedsAnswer',status_date=?,updated_at=? WHERE id=?",(stamp,stamp,job_id))
        db.execute('INSERT INTO state_events(job_id,at,from_state,to_state,actor,reason,evidence_path) VALUES(?,?,?,?,?,?,?)',
                   (job_id,stamp,row['job_state'],'NeedsAnswer',worker_id,reason,evidence_path))
        db.commit()
    except Exception:db.rollback();raise

def record_evidence(db,job_id,kind,path,*,captured_at=None):
    if kind not in ('resume_review','form_review','original_jd','submission_confirmation','response'):
        raise ValueError('Unsupported evidence kind')
    job=db.execute('SELECT slug FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not job or not job['slug']:raise ValueError('Job folder binding missing')
    artifact=(ROOT/path).resolve();folder=(ROOT/'jobs'/job['slug']).resolve()
    if not artifact.is_relative_to(folder) or not artifact.is_file():raise ValueError('Evidence must belong to exact job folder')
    digest=hashlib.sha256(artifact.read_bytes()).hexdigest()
    db.execute('INSERT OR REPLACE INTO evidence(job_id,kind,path,sha256,captured_at) VALUES(?,?,?,?,?)',
               (job_id,kind,path,digest,captured_at or now()))
    return digest

PILOT_JOB_ID=203
PILOT_REQUISITION='250926037784'
PILOT_FORM_URL='https://docs.google.com/forms/d/e/1FAIpQLSfdRVRzYbnjHtF6IXKFQH9ycSSGJXaInzqZXT9Rq2y6QVsxsg/viewform'
INFOSYS_PILOT_JOB_ID=220
INFOSYS_PILOT_REQUISITION='190826038395'
INFOSYS_PILOT_FORM_URL='https://www.naukri.com/job-listings-java-backend-developer-spring-boot-microservices-infosys-pune-chennai-bengaluru-2-to-5-years-190826038395'
AGILIAD_PILOT_JOB_ID=241
AGILIAD_PILOT_REQUISITION='280926023134'
AGILIAD_PILOT_FORM_URL='https://www.naukri.com/job-listings-java-developer-senior-java-developer-agiliad-pune-2-to-6-years-280926023134'
INFOSYS_PILOT_CHECKS={'listing_live','not_previously_applied','jd_reviewed','resume_visually_reviewed',
                      'exact_resume_selected','answers_confirmed','final_submit_identified'}

def _scoped_pilot_allowed(job,form_url,config,test_mode):
    """The user authorized this single Medium portal form; the timer stays read-only."""
    if test_mode or os.environ.get('JOB_SEARCH_TEST_MODE')=='1':return False
    if config['mode']!='read_only' or config['submissions']['enabled'] or not job:return False
    if job['source'].lower()!='naukri_portal_native' or job['fit']!='Medium':return False
    return ((job['id']==PILOT_JOB_ID and job['requisition_id']==PILOT_REQUISITION
             and job['state']=='InProgress' and form_url==PILOT_FORM_URL)
            or (job['id']==INFOSYS_PILOT_JOB_ID and job['requisition_id']==INFOSYS_PILOT_REQUISITION
                and job['state']=='Prepared' and form_url==INFOSYS_PILOT_FORM_URL)
            or (job['id']==AGILIAD_PILOT_JOB_ID and job['requisition_id']==AGILIAD_PILOT_REQUISITION
                and job['state']=='Prepared' and form_url==AGILIAD_PILOT_FORM_URL)
           )

def begin_attempt(db,job_id,resume_path,resume_sha,form_url,form_evidence_path,form_sha,*,test_mode=False,config=None,pilot=False,naukri_worker=False,portal_worker=None,bootstrap=False):
    from job_config import load
    from fit_engine import evaluate
    config=config or load()
    provider=portal_worker or ('naukri' if naukri_worker else None)
    if provider=='linkedin':
        import job_config,control
        if control.is_paused() or not job_config.live_provider_enabled(config,'linkedin',test_mode=test_mode):raise ValueError('LinkedIn live claim disabled')
        if not bootstrap and not portal_path_verified(db,'linkedin'):raise ValueError('LinkedIn path proof missing')
    if pilot:raise ValueError('Legacy pilot bypass retired; use the explicitly configured Naukri live lane')
    if naukri_worker:
        from job_config import live_naukri_enabled
        import control
        if control.is_paused():raise ValueError('Paused workflow cannot claim an application')
        if (os.environ.get('JOB_SEARCH_TEST_MODE')=='1' or not live_naukri_enabled(config,test_mode=test_mode)):
            raise ValueError('Scheduled Naukri worker is disabled or running in test mode')
        if not bootstrap and not naukri_worker_path_verified(db):raise ValueError('Direct Naukri application path lacks a valid job-bound proof marker')
    elif not pilot and provider!='linkedin':require_writable_action(test_mode,config)
    db.execute('BEGIN IMMEDIATE')
    try:
        job=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if pilot:
            if not _scoped_pilot_allowed(job,form_url,config,test_mode):
                raise ValueError('This exact user-authorized portal pilot is not eligible')
            review=(ROOT/form_evidence_path).resolve()
            if not review.is_file():raise ValueError('Pilot pre-submit review missing')
            packet=json.loads(review.read_text())
            expected_requisition = (INFOSYS_PILOT_REQUISITION if job_id==INFOSYS_PILOT_JOB_ID else
                                    AGILIAD_PILOT_REQUISITION if job_id==AGILIAD_PILOT_JOB_ID else PILOT_REQUISITION)
            checks=packet.get('checks',{})
            if packet.get('job_id')!=job_id or packet.get('portal_job_id')!=expected_requisition or packet.get('form_url')!=form_url or not all(checks.values()) or len(checks)!=7:
                raise ValueError('Pilot pre-submit checks are incomplete or misbound')
            if job_id==INFOSYS_PILOT_JOB_ID and (set(checks)!=INFOSYS_PILOT_CHECKS or resume_path!='jobs/naukri-190826038395/Yugal_Teotia_Java_Backend_Developer.pdf'):
                raise ValueError('Infosys pilot checks or exact role resume missing')
            if job_id==INFOSYS_PILOT_JOB_ID:
                expected={'provider':'naukri_native_chat','profile_resume_filename':'Yugal_Teotia_Java_Backend_Developer.pdf',
                          'profile_resume_sha256':resume_sha,'screening_question':'Are you available for a interview?(Virtual)',
                          'screening_answer_confirmed':True,'potential_submit_action':'screening_chat_save'}
                if any(packet.get(key)!=value for key,value in expected.items()):
                    raise ValueError('Infosys native-Apply pre-submit facts are missing or misbound')
                if packet.get('signed_in') is not True or packet.get('account_name_matches') is not True or packet.get('exact_listing_id')!=INFOSYS_PILOT_REQUISITION:
                    raise ValueError('Infosys signed-in account or exact listing was not verified')
                for key in ('profile_verified_at','listing_checked_at'):
                    try:
                        age=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(packet[key].replace('Z','+00:00'))).total_seconds()
                    except (KeyError,TypeError,ValueError,AttributeError) as exc:
                        raise ValueError('Infosys pre-submit time evidence missing') from exc
                    if age<0 or age>900:
                        raise ValueError('Infosys pre-submit evidence is stale')
            if job_id==AGILIAD_PILOT_JOB_ID:
                expected={'provider':'naukri_native_chat','profile_resume_filename':'Yugal_Teotia_Java_Backend_Developer.pdf',
                          'profile_resume_sha256':resume_sha,
                          'screening_question':'How many years of experience do you have in Backend?',
                          'screening_answer_confirmed':True,
                          'potential_submit_action':'screening_chat_save'}
                if (set(checks)!=INFOSYS_PILOT_CHECKS or
                    resume_path!='jobs/agiliad-java-backend-developer-280926023134/Yugal_Teotia_Java_Backend_Developer.pdf' or
                    any(packet.get(key)!=value for key,value in expected.items()) or
                    packet.get('signed_in') is not True or packet.get('account_name_matches') is not True or
                    packet.get('exact_listing_id')!=AGILIAD_PILOT_REQUISITION):
                    raise ValueError('Agiliad native-Apply pre-submit facts are missing or misbound')
                for key in ('profile_verified_at','listing_checked_at','history_checked_at'):
                    try:age=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(packet[key].replace('Z','+00:00'))).total_seconds()
                    except (KeyError,TypeError,ValueError,AttributeError) as exc:
                        raise ValueError('Agiliad pre-submit time evidence missing') from exc
                    if age<0 or age>900:raise ValueError('Agiliad pre-submit evidence is stale')
        else:
            if provider:
                if not job or job['state'] not in ('Prepared','ResumeReady') or job['source']!=provider+'_portal_native':
                    raise ValueError('Only a staged Naukri job may claim submission')
            elif not job or job['state']!='Prepared' or job['fit']!='High':
                raise ValueError('Only prepared High-fit jobs may claim submission')
            saved=json.loads(job['fit_json'] or '{}')
            verdict=evaluate(dict(job),saved.get('decision') or {},config,ROOT/'master-resume/Yugal_Teotia_CV.tex')
            high=job['fit']=='High' and verdict['eligible']
            medium=(provider is not None and job['fit']=='Medium' and verdict.get('strong_medium_eligible')
                    and saved.get('result',{}).get('strong_medium_eligible') is True)
            if not (high or medium):raise ValueError('Current JD no longer passes High or approved strong-Medium evidence gates')
        if not job['requisition_id']:raise ValueError('Exact employer requisition ID required')
        if db.execute('SELECT 1 FROM history_keys WHERE employer_key=? AND requisition_key=?',(job['employer_key'],job['requisition_key'])).fetchone():
            raise ValueError('Historical application key blocks submission')
        if db.execute('SELECT 1 FROM submission_attempts WHERE job_id=?',(job_id,)).fetchone():
            raise ValueError('Existing or uncertain attempt blocks retry')
        if not resume_path or not re.fullmatch(r'[0-9a-f]{64}',resume_sha) or not form_url or not form_evidence_path or not re.fullmatch(r'[0-9a-f]{64}',form_sha):
            raise ValueError('Exact resume/form hash binding required')
        artifact=(ROOT/resume_path).resolve()
        folder=(ROOT/'jobs'/str(job['slug'])).resolve()
        if not job['slug'] or not artifact.is_relative_to(folder) or not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest()!=resume_sha:
            raise ValueError('Resume file/hash mismatch')
        if urlsplit(form_url).scheme!='https' or not urlsplit(form_url).hostname:
            raise ValueError('HTTPS form URL required')
        for kind,path,digest in (('resume_review',resume_path,resume_sha),('form_review',form_evidence_path,form_sha)):
            proof=db.execute('SELECT 1 FROM evidence WHERE job_id=? AND kind=? AND path=? AND sha256=?',
                             (job_id,kind,path,digest)).fetchone()
            if not proof:raise ValueError('Missing exact job-bound '+kind)
        form_file=(ROOT/form_evidence_path).resolve()
        if not form_file.is_relative_to(folder) or hashlib.sha256(form_file.read_bytes()).hexdigest()!=form_sha:
            raise ValueError('Form evidence changed or crossed jobs')
        if naukri_worker:
            packet=json.loads(form_file.read_text())
            checks=packet.get('checks',{})
            if (packet.get('job_id')!=job_id or str(packet.get('requisition_id'))!=str(job['requisition_id']) or
                packet.get('form_url')!=form_url or packet.get('resume_sha256')!=resume_sha or
                packet.get('provider')!='naukri_native_chat' or packet.get('signed_in') is not True or
                packet.get('account_name_matches') is not True or packet.get('resume_filename')!=Path(resume_path).name or
                packet.get('final_action') not in ('screening_chat_save','native_apply_open') or not isinstance(checks,dict) or
                set(checks)!={'live','unapplied','fit','resume_visual','resume_selected','answers_confirmed','unique_apply'} or
                not all(value is True for value in checks.values())):
                raise ValueError('Generic Naukri pre-submit packet failed exact job/resume/answer checks')
        if provider=='linkedin':
            packet=json.loads(form_file.read_text());checks=packet.get('checks',{})
            if (packet.get('job_id')!=job_id or str(packet.get('requisition_id'))!=str(job['requisition_id'])
                    or packet.get('provider')!='linkedin_easy_apply' or packet.get('resume_sha256')!=resume_sha
                    or packet.get('form_url')!=form_url or urlsplit(form_url).hostname!='www.linkedin.com'
                    or urlsplit(form_url).path.rstrip('/')!='/jobs/view/'+str(job['requisition_id'])
                    or not all(checks.get(x) is True for x in ('live','unapplied','fit','resume_visual','resume_selected','answers_confirmed','unique_apply'))):
                raise ValueError('LinkedIn exact job/resume/form claim binding failed')
        attempt_id=str(uuid.uuid4());stamp=now()
        db.execute('''INSERT INTO submission_attempts(attempt_id,job_id,requisition_key,resume_path,resume_sha256,
          form_url,form_evidence_path,form_sha256,state,claimed_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
          (attempt_id,job_id,job['requisition_key'],resume_path,resume_sha,form_url,form_evidence_path,form_sha,'uncertain',stamp))
        db.execute("UPDATE jobs SET state='SubmissionUnconfirmed',status_date=?,updated_at=? WHERE id=?",(stamp,stamp,job_id))
        db.execute('INSERT INTO state_events(job_id,at,from_state,to_state,actor,reason) VALUES(?,?,?,?,?,?)',
                   (job_id,stamp,job['state'],'SubmissionUnconfirmed','submission_gate','One authorized portal pilot claim before final Submit' if pilot else 'Claimed before network action'))
        db.commit();return attempt_id
    except Exception:db.rollback();raise

def confirm_attempt(db,attempt_id,job_id,proof_path,proof_time,*,test_mode=False,config=None,pilot=False,naukri_worker=False,portal_worker=None,bootstrap=False,reconcile_only=False):
    if pilot:
        from job_config import load
        config=config or load()
        row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if (test_mode or os.environ.get('JOB_SEARCH_TEST_MODE')=='1' or config['mode']!='read_only'
                or config['submissions']['enabled'] or not row
                or not ((row['id']==PILOT_JOB_ID and row['requisition_id']==PILOT_REQUISITION)
                        or (row['id']==INFOSYS_PILOT_JOB_ID and row['requisition_id']==INFOSYS_PILOT_REQUISITION)
                        or (row['id']==AGILIAD_PILOT_JOB_ID and row['requisition_id']==AGILIAD_PILOT_REQUISITION))
                or row['state']!='SubmissionUnconfirmed'):
            raise ValueError('This exact portal pilot confirmation is not eligible')
    elif naukri_worker:
        from job_config import load
        config=config or load()
        row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        from job_config import live_naukri_enabled
        if (test_mode or os.environ.get('JOB_SEARCH_TEST_MODE')=='1' or (not reconcile_only and not live_naukri_enabled(config,test_mode=test_mode)) or not row or
            row['source']!='naukri_portal_native' or row['state'] not in (('SubmissionUnconfirmed','NeedsAnswer','InProgress') if reconcile_only else ('SubmissionUnconfirmed',))):
            raise ValueError('Scheduled Naukri confirmation is disabled, test-only, or job-mismatched')
        if not bootstrap and not reconcile_only and not naukri_worker_path_verified(db):raise ValueError('Direct Naukri application path lacks a valid job-bound proof marker')
    elif portal_worker=='linkedin':
        import job_config
        config=config or job_config.load();row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if test_mode or (not reconcile_only and not job_config.live_provider_enabled(config,'linkedin',test_mode=test_mode)) or not row or row['source']!='linkedin_portal_native' or row['state']!='SubmissionUnconfirmed':
            raise ValueError('LinkedIn confirmation disabled or mismatched')
    else:require_writable_action(test_mode,config)
    job_folder=db.execute('SELECT slug,requisition_id FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not job_folder or not job_folder['slug']:raise ValueError('Job folder binding missing')
    artifact=(ROOT/proof_path).resolve()
    if not artifact.is_relative_to((ROOT/'jobs'/job_folder['slug']).resolve()) or not artifact.is_file():
        raise ValueError('Confirmation proof must be saved in the exact job folder')
    proof=json.loads(artifact.read_text())
    if portal_worker=='linkedin' and not (proof.get('application_sent') is True and proof.get('exact_job_id_match') is True
            and urlsplit(str(proof.get('url') or '')).path.rstrip('/')=='/jobs/view/'+str(job_folder['requisition_id'])):
        raise ValueError('LinkedIn requires positive application-sent confirmation tied to the exact job')
    if proof.get('exact_history_id_match') is True and proof.get('history_full') is not True:
        raise ValueError('A positive history-match claim requires a complete Applied Jobs history load')
    if naukri_worker and not (proof.get('exact_history_id_match') is True and proof.get('history_full') is True):
        raise ValueError('Naukri confirmation requires fully loaded exact-ID history; listing-only outcomes remain pending')
    if reconcile_only:
        identity=proof.get('history_identity',{})
        positive=any(str(r.get('job_id'))==str(job_folder['requisition_id']) and
                     any(s.get('statusValue')=='Application Sent' for s in r.get('status',[]))
                     for r in identity.get('server_rows',[]))
        naukri_positive=(naukri_worker and proof.get('account_identity_verified') is True and identity.get('basis')=='server_history_exact_job_id' and positive)
        linkedin_positive=(portal_worker=='linkedin' and proof.get('account_session_verified') is True and
            proof.get('history_filter_verified') is True and proof.get('history_full') is True and proof.get('exact_history_id_match') is True and
            any(str(r.get('job_id'))==str(job_folder['requisition_id']) and r.get('application_sent') is True for r in identity.get('server_rows',[])))
        if not (proof.get('new_application_action') is False and (naukri_positive or linkedin_positive)):
            raise ValueError('Read-only reconciliation requires verified account and exact server Application Sent evidence')
    if proof.get('attempt_id')!=attempt_id or str(proof.get('requisition_id'))!=str(job_folder['requisition_id']) or proof.get('captured_at')!=proof_time:
        raise ValueError('Confirmation proof does not bind to attempt/requisition/time')
    if not proof.get('confirmation_text') and not proof.get('application_id'):
        raise ValueError('Confirmation proof needs a visible acknowledgment or application ID')
    if urlsplit(str(proof.get('url') or '')).scheme!='https':
        raise ValueError('Confirmation proof needs its exact HTTPS page URL')
    _time=dt.datetime.fromisoformat(proof_time.replace('Z','+00:00'))
    if not _time.tzinfo:raise ValueError('Proof time needs timezone')
    digest=hashlib.sha256(artifact.read_bytes()).hexdigest()
    db.execute('BEGIN IMMEDIATE')
    try:
        attempt=db.execute('SELECT * FROM submission_attempts WHERE attempt_id=? AND job_id=?',(attempt_id,job_id)).fetchone()
        if not attempt or attempt['state'] not in (('uncertain','incomplete') if reconcile_only else ('uncertain',)):raise ValueError('No matching pending submission attempt')
        if _time<dt.datetime.fromisoformat(attempt['claimed_at']):raise ValueError('Proof predates attempt')
        job=db.execute('SELECT state,requisition_key FROM jobs WHERE id=?',(job_id,)).fetchone()
        if job['state'] not in (('SubmissionUnconfirmed','NeedsAnswer','InProgress') if reconcile_only else ('SubmissionUnconfirmed',)) or job['requisition_key']!=attempt['requisition_key']:
            raise ValueError('Job/requisition no longer matches attempt')
        if job['state']!='SubmissionUnconfirmed':
            db.execute("UPDATE jobs SET state='SubmissionUnconfirmed' WHERE id=?",(job_id,))
            db.execute('INSERT INTO state_events(job_id,at,from_state,to_state,actor,reason,evidence_path) VALUES(?,?,?,?,?,?,?)',
                       (job_id,now(),job['state'],'SubmissionUnconfirmed','read_only_reconciliation','Positive exact history observed; no application action',proof_path))
        db.execute("UPDATE submission_attempts SET state='confirmed',proof_path=?,proof_sha256=?,proof_captured_at=? WHERE attempt_id=?",
                   (proof_path,digest,proof_time,attempt_id))
        db.execute('INSERT OR REPLACE INTO evidence(job_id,kind,path,sha256,captured_at,attempt_id) VALUES(?,?,?,?,?,?)',
                   (job_id,'submission_confirmation',proof_path,digest,proof_time,attempt_id))
        db.commit()
    except Exception:db.rollback();raise
    return transition(db,job_id,'Applied','submission_confirmation','Exact attempt confirmation',proof_path,'SubmissionUnconfirmed')
