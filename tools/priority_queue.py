"""Durable, fail-closed requisition queue for bounded parallel work."""
import datetime as dt
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from local_paths import ROOT

DB=ROOT/'.workflow/priority-queue.sqlite3'
ACTIVE=('resuming','applying','submitting')
DONE=('submitted','blocked','discarded','needs_review')

def connect(path=None):
    path=Path(path or DB);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=20,isolation_level=None)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA busy_timeout=20000')
    db.executescript('''CREATE TABLE IF NOT EXISTS jobs(
      id INTEGER PRIMARY KEY, company_key TEXT NOT NULL, requisition_key TEXT NOT NULL,
      slug TEXT NOT NULL UNIQUE, url TEXT NOT NULL, ats_host TEXT NOT NULL,
      priority REAL NOT NULL, observed_at TEXT NOT NULL, queued_at TEXT NOT NULL,
      state TEXT NOT NULL DEFAULT 'queued', worker_id TEXT, claimed_at TEXT,
      stage_started TEXT, stage_times TEXT NOT NULL DEFAULT '{}',
      last_error_kind TEXT, UNIQUE(company_key,requisition_key));
      CREATE INDEX IF NOT EXISTS jobs_priority ON jobs(state,priority DESC,observed_at DESC);
      CREATE TABLE IF NOT EXISTS site_cooldowns(host TEXT PRIMARY KEY,until TEXT NOT NULL,reason TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY,job_id INTEGER NOT NULL,time TEXT NOT NULL,
      event TEXT NOT NULL,worker_id TEXT,details TEXT NOT NULL DEFAULT '{}');''')
    return db

def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def host(url):
    p=urlsplit(url)
    if p.scheme!='https' or not p.hostname:raise ValueError('Original HTTPS application URL required')
    return p.hostname.lower()
def clean_key(value):
    import re
    return re.sub(r'[^a-z0-9]','',str(value or '').lower())

def enqueue(slug,company,req,url,priority,observed_at,path=None):
    if not req:raise ValueError('Employer requisition ID required for parallel queue')
    from workflow import employer
    if not 0<=priority<=100:raise ValueError('Priority out of range')
    db=connect(path)
    try:
        db.execute('BEGIN IMMEDIATE')
        key=(employer(company),clean_key(req))
        existing=db.execute('SELECT * FROM jobs WHERE company_key=? AND requisition_key=?',key).fetchone()
        if existing:
            if existing['slug']!=slug or existing['url']!=url:
                raise ValueError('Requisition already queued with different slug/URL; review alias before action')
            if existing['state']=='queued':
                db.execute('UPDATE jobs SET priority=max(priority,?),observed_at=max(observed_at,?) WHERE id=?',(priority,observed_at,existing['id']))
            db.commit();return existing['id']
        cur=db.execute('INSERT INTO jobs(company_key,requisition_key,slug,url,ats_host,priority,observed_at,queued_at) VALUES(?,?,?,?,?,?,?,?)',(*key,slug,url,host(url),priority,observed_at,now()))
        db.execute('INSERT INTO history(job_id,time,event) VALUES(?,?,?)',(cur.lastrowid,now(),'enqueued'))
        db.commit();return cur.lastrowid
    except Exception:
        db.rollback();raise
    finally:db.close()

def claim(worker_id,path=None,stage='resume'):
    if not worker_id or len(worker_id)>80:raise ValueError('Invalid worker ID')
    if stage not in ('resume','form'):raise ValueError('Invalid worker stage')
    source='queued' if stage=='resume' else 'resume_ready'
    target='resuming' if stage=='resume' else 'applying'
    db=connect(path)
    try:
        db.execute('BEGIN IMMEDIATE')
        # A stale claim is intentionally NOT recycled: the browser may have submitted.
        for row in db.execute('SELECT * FROM jobs WHERE state=? ORDER BY priority DESC,observed_at DESC,id LIMIT 50',(source,)):
            if db.execute('SELECT 1 FROM site_cooldowns WHERE host=? AND until>?',(row['ats_host'],now())).fetchone():continue
            if db.execute("SELECT 1 FROM jobs WHERE state IN ('resuming','applying','submitting') AND (company_key=? OR ats_host=?)",(row['company_key'],row['ats_host'])).fetchone():continue
            stamp=now()
            db.execute('UPDATE jobs SET state=?,worker_id=?,claimed_at=?,stage_started=? WHERE id=? AND state=?',(target,worker_id,stamp,stamp,row['id'],source))
            db.execute('INSERT INTO history(job_id,time,event,worker_id) VALUES(?,?,?,?)',(row['id'],stamp,target,worker_id))
            db.commit();return dict(db.execute('SELECT * FROM jobs WHERE id=?',(row['id'],)).fetchone())
        db.commit();return None
    except Exception:db.rollback();raise
    finally:db.close()

def transition(job_id,worker_id,state,details=None,path=None):
    if state not in ('resume_ready','submitting','submitted','blocked','discarded','needs_review'):
        raise ValueError('Unsupported state')
    db=connect(path)
    try:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not row or row['worker_id']!=worker_id or row['state'] not in ACTIVE:
            raise ValueError('Only the claiming worker may advance an active requisition')
        if state=='resume_ready' and row['state']!='resuming':raise ValueError('Resume stage mismatch')
        if state=='submitting' and row['state']!='applying':raise ValueError('Prepare before submission')
        if state=='submitted' and row['state']!='submitting':raise ValueError('Submission proof stage missing')
        stamp=now();times=json.loads(row['stage_times'])
        elapsed=(dt.datetime.fromisoformat(stamp)-dt.datetime.fromisoformat(row['stage_started'])).total_seconds()
        times[row['state']]=round(elapsed,3)
        db.execute('UPDATE jobs SET state=?,worker_id=?,stage_started=?,stage_times=? WHERE id=?',(state,None if state=='resume_ready' else worker_id,stamp,json.dumps(times),job_id))
        db.execute('INSERT INTO history(job_id,time,event,worker_id,details) VALUES(?,?,?,?,?)',(job_id,stamp,state,worker_id,json.dumps(details or {})))
        db.commit()
    except Exception:db.rollback();raise
    finally:db.close()

def cooldown(hostname,seconds,reason,path=None):
    if not 1<=seconds<=86400 or reason not in ('rate_limit','site_failure','verification_required'):
        raise ValueError('Invalid site cooldown')
    until=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=seconds)).isoformat()
    db=connect(path)
    try:db.execute('INSERT INTO site_cooldowns VALUES(?,?,?) ON CONFLICT(host) DO UPDATE SET until=excluded.until,reason=excluded.reason',(hostname,until,reason))
    finally:db.close()

def status(path=None):
    db=connect(path)
    try:
        return {'jobs':[dict(r) for r in db.execute('SELECT id,slug,company_key,requisition_key,url,priority,state,worker_id,stage_times FROM jobs ORDER BY priority DESC,observed_at DESC')],
                'counts':{r['state']:r['n'] for r in db.execute('SELECT state,count(*) AS n FROM jobs GROUP BY state')}}
    finally:db.close()
