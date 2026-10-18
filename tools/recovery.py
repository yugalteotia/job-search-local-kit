"""Startup crash recovery for queue claims and interrupted (unclaimed) Naukri preparation.

Runs while the pipeline holds the run lock, so no live worker can own these rows.
It never touches a claimed attempt: SubmissionUnconfirmed / uncertain rows stay locked.
"""
import datetime as dt

import job_db

def recover_jobs(db,stale_minutes=15,now=None):
    now=now or dt.datetime.now(dt.timezone.utc)
    cut=(now-dt.timedelta(minutes=stale_minutes)).isoformat()
    report={'requeued_resume':0,'returned_to_prepared':0,'claimed_left_locked':0}
    for row in db.execute("SELECT job_id FROM queue WHERE state='Resuming' AND (stage_started IS NULL OR stage_started<?)",(cut,)).fetchall():
        db.execute("UPDATE queue SET state='QueuedResume',worker_id=NULL,stage_started=NULL WHERE job_id=? AND state='Resuming'",(row['job_id'],))
        report['requeued_resume']+=1
    for row in db.execute("""SELECT j.id,j.state FROM jobs j WHERE j.source='naukri_portal_native' AND j.state IN ('InProgress','Preparing')
        AND j.updated_at<? AND NOT EXISTS(SELECT 1 FROM submission_attempts a WHERE a.job_id=j.id)""",(cut,)).fetchall():
        try:
            job_db.transition(db,row['id'],'Prepared','recovery','Interrupted before any claim; checks will rerun',None,row['state'])
            report['returned_to_prepared']+=1
        except ValueError:pass
    # A continuation that died mid-flight is an in-flight claim: lock it as unconfirmed (never retried).
    for row in db.execute("""SELECT j.id FROM jobs j JOIN submission_attempts a ON a.job_id=j.id
        WHERE j.state='InProgress' AND a.state='uncertain' AND j.updated_at<?""",(cut,)).fetchall():
        try:
            job_db.transition(db,row['id'],'SubmissionUnconfirmed','recovery','Continuation was interrupted; outcome must be verified',None,'InProgress')
            report['interrupted_continuations_locked']=report.get('interrupted_continuations_locked',0)+1
        except ValueError:pass
    report['claimed_left_locked']=db.execute("SELECT count(*) FROM submission_attempts a JOIN jobs j ON j.id=a.job_id WHERE a.state='uncertain' AND j.state='SubmissionUnconfirmed'").fetchone()[0]
    return report
