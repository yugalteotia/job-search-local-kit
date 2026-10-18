"""Read-only exact-history reconciliation. Called only inside the shared run lock."""
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from local_paths import ROOT
import job_db

DDL='''CREATE TABLE IF NOT EXISTS application_confirmation_notifications(
 key TEXT PRIMARY KEY,job_id INTEGER NOT NULL,attempt_id TEXT,proof_path TEXT NOT NULL,
 created_at TEXT NOT NULL,sent_at TEXT,last_error TEXT);'''

def _read(path):
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return {}

def pending(db,provider,job_id=None):
    rows=db.execute('''SELECT j.*,a.attempt_id,a.state AS attempt_state,a.claimed_at,a.resume_sha256,
        a.proof_path AS attempt_proof_path FROM jobs j LEFT JOIN submission_attempts a ON a.job_id=j.id
        WHERE j.source=? AND j.state IN ('NeedsAnswer','InProgress','SubmissionUnconfirmed','Applied')
        ORDER BY j.id''',(provider+'_portal_native',)).fetchall()
    out=[]
    for row in rows:
        row=dict(row)
        if job_id and row['id']!=job_id:continue
        if not row['requisition_id']:continue
        audit=_read(ROOT/'jobs'/row['slug']/'application-outcome-audit.json')
        historical_pending=(audit and audit.get('history_reconciled') is not True and
            (audit.get('independently_reconciled') is False or audit.get('application_origin','').startswith('Unintended')))
        if row['attempt_state'] in ('uncertain','incomplete') or row['state']=='NeedsAnswer' or historical_pending:out.append(row)
    return out

def naukri_history(rows):
    import naukri_native_worker as worker
    from naukri_direct_playwright import _chrome,_status
    from naukri_discovery import readonly_route
    with _chrome(headless=False,read_only=True) as context:
        context.route('**/*',readonly_route);page=context.pages[0] if context.pages else context.new_page()
        health=_status(page);identity=health['signed_in'] and page.evaluate('()=>/Yugal\\s+Teotia/i.test(document.body.innerText)')
        if not identity:return {'full':False,'account_verified':False,'blocker':'naukri_account_identity_or_login_unverified','rows_by_id':{}}
        before=worker.current_filename(page)
        first=rows[0]
        history=worker._history(page,first['requisition_id'],first['employer'],first['title'],exact_ids=[r['requisition_id'] for r in rows])
        _status(page);after=worker.current_filename(page)
        return {'full':history['full'],'account_verified':True,'rows_by_id':history['exact_rows_by_id'],
                'loaded_rows':history['loaded_rows'],'expected_total':history['expected_total'],'load_sweeps':history['load_sweeps'],
                'generic_filename_before':before,'generic_filename_after':after,'expected_generic_filename':worker.GENERIC.name,
                'blocker':None if history['full'] else 'naukri_applied_history_incomplete'}

# LinkedIn history is accepted only with an explicitly selected Applied filter
# and an explicit total that the enumerated exact-ID rows satisfy. Empty shells
# and the new job tracker are NOT treated as a complete application history.
def linkedin_history(rows):
    from linkedin_browser import chrome,status
    from naukri_discovery import readonly_route
    matches={};sweeps=[]
    with chrome(read_only=True) as context:
        context.route('**/*',readonly_route);page=context.pages[0] if context.pages else context.new_page();health=status(page)
        if not health['signed_in']:return {'full':False,'account_verified':False,'rows_by_id':{},'blocker':health.get('error_kind')}
        page.goto('https://www.linkedin.com/my-items/saved-jobs/?cardType=APPLIED',wait_until='domcontentloaded',timeout=30000)
        page.wait_for_timeout(3000)
        for unused in range(25):
            snap=page.evaluate(r'''()=>{
                const visible=e=>!!e&&!!e.getClientRects().length;
                const tabs=[...document.querySelectorAll('a,button,[role=tab]')].filter(visible);
                const applied=tabs.find(e=>/^Applied(?:\s*\(\d+\))?$/i.test(e.innerText.trim())&&
                    (e.getAttribute('aria-selected')==='true'||e.getAttribute('aria-current')==='page'));
                const total=(applied?.innerText||'').match(/\((\d+)\)/);
                const root=document.querySelector('main,[role=main]');const jobs=[];
                for(const card of root?.querySelectorAll('li')||[]){
                    const a=[...card.querySelectorAll('a[href]')].find(e=>/\/jobs\/view\/(?:[^/?]*-)?\d+/.test(e.href));
                    const id=a?.href.match(/\/jobs\/view\/(?:[^/?]*-)?(\d+)/)?.[1];
                    if(id&&/^Applied\b/mi.test(card.innerText))jobs.push({job_id:id,application_sent:true});}
                const next=tabs.find(e=>e.tagName==='BUTTON'&&/next/i.test(e.getAttribute('aria-label')||e.innerText));
                return {filter_verified:!!applied,total:total?Number(total[1]):null,jobs,
                    next_enabled:!!next&&!next.disabled,path:location.pathname};}''')
            sweeps.append(snap)
            if not snap['filter_verified'] or snap['total'] is None:break
            for row in snap['jobs']:matches[row['job_id']]=row
            if len(matches)>=snap['total']:break
            if not snap['next_enabled']:break
            next_button=page.get_by_role('button',name=re.compile('Next',re.I))
            if next_button.count()!=1:break
            next_button.click();page.wait_for_timeout(1500)
        end=sweeps[-1];full=end['filter_verified'] and end['total'] is not None and len(matches)>=end['total']
        return {'full':bool(full),'account_verified':True,'history_filter_verified':end['filter_verified'],
            'rows_by_id':{r['requisition_id']:[matches[r['requisition_id']]] for r in rows if r['requisition_id'] in matches} if full else {},
            'loaded_rows':len(matches),'expected_total':end['total'],'load_sweeps':sweeps,
            'blocker':None if full else 'linkedin_complete_applied_history_not_verified'}

def _positive(row,provider,history):
    if history.get('full') is not True or history.get('account_verified') is not True:return []
    matches=[r for r in history.get('rows_by_id',{}).get(str(row['requisition_id']),[]) if isinstance(r,dict) and str(r.get('job_id'))==str(row['requisition_id'])]
    if provider=='linkedin':
        return [r for r in matches if r.get('application_sent') is True] if history.get('history_filter_verified') is True else []
    valid=[]
    for match in matches:
        for status in match.get('status') or []:
            if not isinstance(status,dict):continue
            if status.get('statusValue')!='Application Sent':continue
            if row.get('claimed_at'):
                try:
                    value=status.get('dateTime') or status.get('modifiedDate')
                    sent=dt.datetime.fromisoformat(value)
                    if not sent.tzinfo:sent=sent.replace(tzinfo=ZoneInfo('Asia/Kolkata'))
                    if sent<dt.datetime.fromisoformat(row['claimed_at']):continue
                except (ValueError,TypeError):continue
            valid.append(match);break
    return valid

def queue_notification(db,job_id,attempt_id,proof_path):
    db.execute(DDL)
    if not attempt_id:
        claim=db.execute('SELECT attempt_id FROM submission_attempts WHERE job_id=?',(job_id,)).fetchone()
        if claim:attempt_id=claim['attempt_id']
    key=attempt_id or 'observed-job-'+str(job_id)
    db.execute('''INSERT INTO application_confirmation_notifications(key,job_id,attempt_id,proof_path,created_at) VALUES(?,?,?,?,?)
                  ON CONFLICT(key) DO UPDATE SET proof_path=excluded.proof_path
                  WHERE application_confirmation_notifications.sent_at IS NULL''',
               (key,job_id,attempt_id,proof_path,job_db.now()))

def deliver_notifications(db):
    import urgent_alerts
    db.execute(DDL);results=[]
    for row in db.execute('''SELECT n.*,j.employer,j.title,j.canonical_url,j.state,a.state AS attempt_state,a.proof_sha256
        FROM application_confirmation_notifications n JOIN jobs j ON j.id=n.job_id
        LEFT JOIN submission_attempts a ON a.attempt_id=n.attempt_id WHERE n.sent_at IS NULL''').fetchall():
        if row['state']!='Applied' or (row['attempt_id'] and row['attempt_state']!='confirmed'):continue
        proof=ROOT/row['proof_path']
        if not proof.is_file():continue
        if row['attempt_id'] and hashlib.sha256(proof.read_bytes()).hexdigest()!=row['proof_sha256']:continue
        try:
            notice=urgent_alerts.desktop('Application confirmed',row['employer']+' — '+row['title']+'\n'+row['canonical_url'])
            if notice.get('notification_sent'):
                db.execute('UPDATE application_confirmation_notifications SET sent_at=?,last_error=NULL WHERE key=?',(job_db.now(),row['key']))
            else:db.execute('UPDATE application_confirmation_notifications SET last_error=? WHERE key=?',('Desktop notification unavailable',row['key']))
            results.append({'job_id':row['job_id'],'sent':bool(notice.get('notification_sent'))})
        except Exception as exc:
            db.execute('UPDATE application_confirmation_notifications SET last_error=? WHERE key=?',(type(exc).__name__,row['key']))
            results.append({'job_id':row['job_id'],'sent':False,'error_type':type(exc).__name__})
    return results

def refresh_tracker(db):
    import job_views
    return job_views.regenerate(ROOT,database=db.execute('PRAGMA database_list').fetchone()['file'])

def _confirm(db,row,provider,history,matches,config,phase):
    folder=ROOT/'jobs'/row['slug'];folder.mkdir(parents=True,exist_ok=True);stamp=job_db.now()
    progress_path=folder/('naukri-native-progress.json' if provider=='naukri' else 'linkedin-easy-apply-progress.json');progress=_read(progress_path)
    proof={'attempt_id':row['attempt_id'],'requisition_id':row['requisition_id'],'captured_at':stamp,'url':row['canonical_url'],
        'confirmation_text':'Fully loaded exact-ID Applied Jobs history confirms Application Sent',
        'history_full':True,'exact_history_id_match':True,'history_loaded_rows':history.get('loaded_rows'),
        'history_expected_total':history.get('expected_total'),'account_identity_verified':provider=='naukri' and history['account_verified'],
        'account_session_verified':history['account_verified'],'history_filter_verified':history.get('history_filter_verified',True),
        'history_identity':{'basis':'server_history_exact_job_id' if provider=='naukri' else 'exact_linkedin_applied_history','server_rows':matches},
        'new_application_action':False,'saved_answer_replayed':False,'read_only_reconciliation':True,'automatic_reconciliation':True,'phase':phase,
        'application_sent':True,'exact_job_id_match':True,'workflow_worker_proven':False,
        'confirmation_origin':'read_only_history_reconciliation'}
    proof.update(unverified_pending_question=progress.get('pending_question'),
                 screening_answer_delivery_not_verified_by_history=True)
    path=folder/(provider+'-history-reconciliation-confirmation.json');path.write_text(json.dumps(proof,indent=2)+'\n');rel=str(path.relative_to(ROOT))
    queue_notification(db,row['id'],row['attempt_id'],rel)
    if row['attempt_state'] in ('uncertain','incomplete'):
        job_db.confirm_attempt(db,row['attempt_id'],row['id'],rel,stamp,config=config,naukri_worker=provider=='naukri',
                              portal_worker='linkedin' if provider=='linkedin' else None,reconcile_only=True)
    elif row['state']!='Applied':
        if provider!='naukri':raise ValueError('Unclaimed LinkedIn history import is not supported')
        job_db.record_observed_naukri_application(db,row['id'],rel)
    else:
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if row['attempt_id']:
            db.execute('UPDATE submission_attempts SET proof_path=?,proof_sha256=?,proof_captured_at=? WHERE attempt_id=? AND state=\'confirmed\'',(rel,digest,stamp,row['attempt_id']))
        db.execute('INSERT OR REPLACE INTO evidence(job_id,kind,path,sha256,captured_at,attempt_id) VALUES(?,?,?,?,?,?)',
                   (row['id'],'submission_confirmation',rel,digest,stamp,row['attempt_id']))
    import urgent_alerts
    urgent_alerts.resolve_sqlite_confirmed_blocker(db,row['id'],observed_proof_path=rel if not row['attempt_id'] else None)
    if provider=='naukri':
        sent=next((s.get('dateTime') or s.get('modifiedDate') for m in matches for s in (m.get('status') or []) if s.get('statusValue')=='Application Sent'),None)
        if sent:
            legacy=json.loads(row['tracker_json'] or '{}')
            legacy.update({'Submission date':sent,'Confirmation evidence':rel,'Next step':'Application confirmed by exact history; monitor responses. Do not reapply.'})
            db.execute('UPDATE jobs SET tracker_json=? WHERE id=?',(json.dumps(legacy),row['id']))
    audit_path=folder/'application-outcome-audit.json';audit=_read(audit_path)
    if audit:
        if 'applied_history_current' in audit:audit.setdefault('historical_applied_history_report',audit['applied_history_current'])
        audit.update(history_reconciled=True,history_confirmation_path=rel,history_reconciled_at=stamp)
        audit.update(applied_history_current='Verified complete exact-ID history',email_reconciliation_performed=False)
        # Preserve historical inspector origin, resume usage and uninspected email discrepancy.
        audit_path.write_text(json.dumps(audit,indent=2)+'\n')
    if progress:
        progress_backup=progress_path.with_name(progress_path.stem+'-before-automatic-reconciliation.json')
        if not progress_backup.exists():progress_backup.write_text(json.dumps(progress,indent=2)+'\n')
        progress.update(status='confirmed',pending_question=None,pending_field=None,pending_reason=None,confirmation_path=rel,updated_at=stamp)
        progress_path.write_text(json.dumps(progress,indent=2)+'\n')
    outcome_path=folder/('naukri-native-outcome.json' if provider=='naukri' else 'linkedin-easy-apply-outcome.json');old=_read(outcome_path)
    if old:
        backup=outcome_path.with_name(outcome_path.stem+'-before-automatic-reconciliation.json')
        if not backup.exists():backup.write_text(json.dumps(old,indent=2)+'\n')
        updated={k:v for k,v in old.items() if k not in ('error_message','error_type','error_origin','pending_question','reason','reconciliation_pending')}
        updated.update(status='Applied',confirmation_path=rel,automatic_reconciliation=True,new_application_action=False,saved_answer_replayed=False)
        outcome_path.write_text(json.dumps(updated,indent=2)+'\n')
    return {'job_id':row['id'],'requisition_id':row['requisition_id'],'attempt_id':row['attempt_id'],'status':'Applied','confirmation_path':rel,
            'new_claim_created':False,'new_application_action':False,'url':row['canonical_url']}

def run(db,config,*,phase,run_id=None,test_mode=False,sources=None,application_job_id=None):
    if test_mode:return {'status':'SkippedDryRun','phase':phase,'confirmed':[],'new_claims':0,'browser_started':False}
    report={'status':'Complete','phase':phase,'providers':{},'confirmed':[],'new_claims':0}
    for provider,reader in [('naukri',naukri_history),('linkedin',linkedin_history)]:
        if sources and provider not in sources:continue
        if not config['sources'][provider]['enabled']:continue
        rows=pending(db,provider,application_job_id)
        if not rows:report['providers'][provider]={'status':'NoPending','pending':0};continue
        try:
            history=reader(rows)
            report['providers'][provider]={'status':'Checked' if history.get('full') else 'Blocked','pending':len(rows),'history':history}
            for row in rows:
                matches=_positive(row,provider,history)
                if matches:
                    try:report['confirmed'].append(_confirm(db,row,provider,history,matches,config,phase))
                    except Exception as exc:
                        report['providers'][provider].setdefault('job_errors',[]).append({'job_id':row['id'],'error_type':type(exc).__name__})
        except Exception as exc:
            report['providers'][provider]={'status':'Blocked','pending':len(rows),'error_type':type(exc).__name__}
    db.execute(DDL)
    undelivered=db.execute("SELECT 1 FROM application_confirmation_notifications n JOIN jobs j ON j.id=n.job_id WHERE n.sent_at IS NULL AND j.state='Applied' LIMIT 1").fetchone()
    if report['confirmed'] or undelivered:report['tracker']=refresh_tracker(db)
    report['notifications']=deliver_notifications(db)
    if run_id:
        path=ROOT/'.workflow/runs'/f'{run_id}-reconciliation-{phase}.json';path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,indent=2)+'\n')
        report['evidence_path']=str(path.relative_to(ROOT))
    return report
