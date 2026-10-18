"""Read the two unclaimed Naukri handoffs; never answer, submit, or retry attempts."""
import fcntl
import json
from pathlib import Path
import job_db
from local_paths import ROOT
from naukri_direct_playwright import _chrome,_status
from infosys_resume_flow import current_filename
from naukri_native_worker import _listing,_history,GENERIC,_now
from naukri_discovery import readonly_route

def inspector_readonly_route(route):
    """Default-deny mutation requests, including native show which can submit."""
    if route.request.method not in ('GET','HEAD','OPTIONS'):
        route.abort()
    else:
        readonly_route(route)

def run():
    with (ROOT/'.workflow/daily-run.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        db=job_db.connect();results=[]
        try:
            with _chrome(headless=False,read_only=True) as context:
                context.route('**/*',inspector_readonly_route)
                page=context.pages[0] if context.pages else context.new_page()
                if not _status(page)['signed_in']:raise RuntimeError('Naukri login required in isolated Chrome')
                if current_filename(page)!=GENERIC.name:raise RuntimeError('Generic resume is not active; use the live restore path')
                for job_id in (202,266):
                    job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
                    if db.execute('SELECT 1 FROM submission_attempts WHERE job_id=?',(job_id,)).fetchone():
                        results.append({'job_id':job_id,'status':'ExistingAttemptPreserved'});continue
                    req=str(job['requisition_id']);url=job['canonical_url']
                    listing=_listing(page,url,req);history=_history(page,req,job['employer'],job['title'])
                    record={'job_id':job_id,'requisition_id':req,'listing':listing,'history':history,'at':_now(),
                            'answers_sent':0,'application_completion_sent':False,'generic_resume_restored':True}
                    # Native "show" can commit jobs without screening questions.
                    # A read-only inspection must never click Apply or send that request.
                    record['status']='AlreadyApplied' if listing['applied'] else 'UnclaimedQuestionUnknown'
                    record['read_only']=True
                    folder=ROOT/'jobs'/job['slug'];(folder/'naukri-handoff-inspection.json').write_text(json.dumps(record,indent=2)+'\n')
                    results.append(record)
            return results
        finally:db.close()

if __name__=='__main__':
    try:print(json.dumps(run(),indent=2))
    except Exception as exc:print(json.dumps({'error_type':type(exc).__name__,'handoff':'Isolated Naukri session or listing could not be verified'}));raise SystemExit(1)
