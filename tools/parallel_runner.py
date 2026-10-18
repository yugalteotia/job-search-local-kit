"""Three bounded discovery/resume workers; one safe browser form worker."""
import concurrent.futures
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import selectors
import subprocess
import time

from local_paths import ROOT
import daily_runner as daily
import priority_queue as queue

STATE=ROOT/'.workflow'
SHARDS={
 'portals_a':'Naukri and Indeed',
 'portals_b':'LinkedIn and Foundit',
 'portals_c':'Cutshort, relevant original employer career boards, saved portal alerts and Gmail job-alert metadata',
}

def _run_agent(w,kind,name,prompt,budget,env=None):
    log=STATE/'runs'/f'{name}.jsonl';log.parent.mkdir(parents=True,exist_ok=True)
    started=time.monotonic()
    def record(event,**data):
        with log.open('a') as file:file.write(json.dumps({'time':w.now(),'event':event,**data})+'\n')
    record('started',kind=kind,budget_seconds=budget)
    run_id=name.split('-',1)[0]
    child=subprocess.Popen(daily.cli_command(),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                           text=True,bufsize=1,start_new_session=True,
                           env={**os.environ,**(env or {}),'JOB_SEARCH_RUN_ID':run_id})
    child.stdin.write(prompt);child.stdin.close()
    selector=selectors.DefaultSelector();selector.register(child.stdout,selectors.EVENT_READ,'stdout');selector.register(child.stderr,selectors.EVENT_READ,'stderr')
    timed_out=False;paused=False;turn_completed=False
    try:
        while selector.get_map():
            if w.read_json(STATE/'control.json',{}).get('paused'):
                paused=True;daily.stop_child(child);break
            if time.monotonic()-started>=budget:
                timed_out=True;daily.stop_child(child);break
            for key,_ in selector.select(timeout=1):
                line=key.fileobj.readline()
                if not line:selector.unregister(key.fileobj);continue
                if key.data=='stderr':continue
                try:event=json.loads(line)
                except ValueError:continue
                item=event.get('item',{})
                if item.get('type')=='mcp_tool_call':record('browser_tool',tool=item.get('tool'),status=item.get('status'))
                elif event.get('type') in ('turn.completed','turn.failed'):
                    turn_completed=event['type']=='turn.completed';record(event['type'])
        code=child.wait(timeout=5)
    finally:
        daily.stop_child(child);selector.close();child.stdout.close();child.stderr.close()
    result={'kind':kind,'name':name,'exit_code':code,'completed':code==0 and turn_completed and not timed_out and not paused,
            'timed_out':timed_out,'paused':paused,'seconds':round(time.monotonic()-started,2),'log':str(log.relative_to(ROOT))}
    record('finished',**result)
    return result

def _discovery_prompt(run_id,shard,coverage):
    return f'''Read SEARCH_PROFILE.md, APPLICATION_PROFILE.md, tracker.xlsx, the generic master resume, existing job records, .workflow/cleanup/other_history_keys.json and DAILY_WORKFLOW_AGENT.md. Discovery shard {shard}: {SHARDS[shard]} ONLY.
This is a normal authorized discovery/verification run, not a read-only probe. Do not open application forms, tailor resumes, upload, or submit in this shard. You own one private Playwright MCP connection. The connection enforces one owned tab per worker and reuses it for later `browser_tabs new` requests. Record the exact numeric tab ID returned by the first `new`; use and close only that recorded ID. Never select or close pre-existing tabs, or infer ownership from URL/title/group. The connection closes its recorded owned tab at worker exit; include created/reused/closed/remaining counts and uncertain IDs in your digest. Search at least two targeted combinations of Java/Spring Boot/backend/API, 1–3 years and relevant Indian locations per reachable portal, with up to two pages/query and actual date sorting when offered. Search newest first; never retry/bypass Indeed's HTTP 403. Inspect original employer pages for promising leads, at most six original requisitions in this shard. Deduplicate against tracker/history before ingest. Save eligible verified original JDs using workflow ingest; strict High requires evidenced essential fit, not keywords. Job alerts are leads, not applications. Keep account data, addresses, message contents, cookies, tokens and OTPs out of artifacts.
Write {coverage} as JSON with keys shard, searches (each actual source, query, page_url, page_number, sort, checked_at, status), original_requisitions (each company, requisition_id, original_url, live_verified boolean, fit or unknown), alerts_checked count, blocked_sources. Never claim a search-result count as page coverage. Write a short shard digest at .workflow/digests/{run_id}-{shard}.md. Stop within 9 minutes. Do not invent coverage or High fit.'''

def _resume_prompt(slug,req,url):
    return f'''You are the resume-only worker for exact slug {slug}, employer requisition {req}, original URL {url}. Read DAILY_WORKFLOW_AGENT.md and the generic master. The worker environment binds all workflow writes to this requisition. Do not use any browser tools, fill forms, upload or submit. Recheck listing.json is live, original-verified High and exact requisition/URL; if not, stop with a clear local reason. Run workflow tailor {slug}; inspect the actual 200-DPI PNG using view_image, check letters, links, margins and bullets, and complete the hash-bound visual review using tools/check_resume_pdf.py with the project-local dependencies. Save only this job's artifacts. Do not claim readiness if visual review fails. Return a short status without private fields.'''

def _form_prompt(slug,req,url):
    return f'''You are the sole browser-application worker for exact slug {slug}, employer requisition {req}, original URL {url}. Read DAILY_WORKFLOW_AGENT.md. The worker environment binds all workflow tracker writes to this requisition. You have a dedicated private MCP connection. Create a NEW dedicated tab at exactly {url}; never select an existing tab or use another worker's tab. Before each form action verify the selected tab is this requisition's original application or its observed same-ATS continuation. If URL or job identity is uncertain, stop this job. Check portal/ATS/Gmail application history conservatively before any form action. Use only the visually reviewed role PDF and confirmed facts. If a required unknown, CAPTCHA, login, file-upload limitation, new host, rate limit, or materially different certification blocks this job, use workflow needs-answer for this slug with a safe packet, save an exact manual action in the digest, and stop this worker; other jobs continue. For a rate limit or ATS failure also run workflow queue-cooldown {slug} rate_limit 1800 or site_failure 900 as appropriate. Never guess or retry an uncertain submission. If the form is fully verified, use workflow begin-submission, click final Submit ONCE and capture actual confirmation via workflow record-confirmation. Never mark Applied without proof. Write a job-only digest at .workflow/digests/worker-{slug}.md. Keep secrets, addresses and form values out of logs.'''

def _identity_env(row):
    return {'JOB_SEARCH_WORKER_SLUG':row['slug'],'JOB_SEARCH_WORKER_COMPANY':row['company_key'],
            'JOB_SEARCH_WORKER_REQ':row['requisition_key'],'JOB_SEARCH_WORKER_URL':row['url']}

def _resume_one(w,run_id,slot,budget):
    row=queue.claim(f'{run_id}-resume-{slot}',stage='resume')
    if not row:return None
    worker=row['worker_id'];slug=row['slug'];folder=w.job_folder(slug)
    listing=w.read_json(folder/'listing.json',{})
    if listing.get('fit')!='High' or w.normalize(listing.get('job_id'))!=row['requisition_key'] or listing.get('url')!=row['url']:
        queue.transition(row['id'],worker,'needs_review',{'reason':'listing identity changed'})
        return {'slug':slug,'stage':'needs_review','reason':'listing identity changed'}
    result=_run_agent(w,'resume',f'{run_id}-resume-{slot}-{slug}',_resume_prompt(slug,row['requisition_key'],row['url']),budget,_identity_env(row))
    try:w.verify_visual_review(folder);ready=result['completed']
    except (ValueError,KeyError,FileNotFoundError):ready=False
    queue.transition(row['id'],worker,'resume_ready' if ready else 'needs_review',{'log':result['log']})
    return {**result,'slug':slug,'stage':'resume_ready' if ready else 'needs_review'}

def _form_one(w,run_id,number,budget):
    row=queue.claim(f'{run_id}-form-{number}',stage='form')
    if not row:return None
    worker=row['worker_id'];slug=row['slug'];folder=w.job_folder(slug)
    result=_run_agent(w,'form',f'{run_id}-form-{number}-{slug}',_form_prompt(slug,row['requisition_key'],row['url'])+
                      '\nThe Playwright connection records your numeric tab ID, reuses that worker-owned tab, blocks other tab IDs, and closes the recorded tab on exit. Report created/reused/closed/remaining counts and uncertain IDs in your digest. Never infer ownership from URL, title, or Playwright group.\n',
                      budget,_identity_env(row))
    if result['timed_out'] or result['exit_code']!=0:
        queue.cooldown(row['ats_host'],900,'site_failure')
    listing=w.read_json(folder/'listing.json',{})
    matched=[r for r in w.tracker_rows()[3] if w.key(r)==(row['company_key'],row['requisition_key'])]
    status=str(matched[0].get('Status','')) if len(matched)==1 else ''
    attempt_data=w.read_json(folder/'submission_attempt.json',{})
    confirmation_data=w.read_json(folder/'confirmation.json',{})
    attempt=bool(attempt_data);proof=bool(confirmation_data)
    try:
        ordered=(dt.datetime.fromisoformat(row['claimed_at'])<=dt.datetime.fromisoformat(attempt_data['started_at'])
                 <=dt.datetime.fromisoformat(confirmation_data['captured_at'].replace('Z','+00:00')))
    except (KeyError,ValueError,TypeError):ordered=False
    exact_proof=(ordered and w.normalize(attempt_data.get('job_id'))==row['requisition_key']
                 and w.normalize(confirmation_data.get('job_id'))==row['requisition_key']
                 and (ROOT/confirmation_data.get('artifact','')).is_file())
    if status=='Applied' and attempt and proof and exact_proof:
        queue.transition(row['id'],worker,'submitting',{'attempt_recorded':True})
        queue.transition(row['id'],worker,'submitted',{'confirmation':str((folder/'confirmation.json').relative_to(ROOT))})
        stage='submitted'
    elif status in ('Needs Answer','Blocked') and not attempt:
        queue.transition(row['id'],worker,'blocked',{'status':status});stage='blocked'
    else:
        if attempt:queue.transition(row['id'],worker,'submitting',{'attempt_recorded':True})
        queue.transition(row['id'],worker,'needs_review',{'status':status,'attempt_recorded':attempt});stage='needs_review'
    return {**result,'slug':slug,'stage':stage}

def run(w,max_seconds=1800):
    started=time.monotonic();run_id=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')
    config=w.read_json(STATE/'parallel-config.json',{})
    discovery_limit=max(1,min(3,int(config.get('discovery_workers',3))))
    resume_limit=max(1,min(3,int(config.get('resume_workers',3))))
    if int(config.get('form_workers',1))!=1 or config.get('parallel_submission_enabled') is True:
        raise ValueError('Parallel form/submission workers are not enabled: two PDF-attachment isolation flows have not passed')
    STATE.mkdir(exist_ok=True);(STATE/'runs').mkdir(exist_ok=True);(STATE/'digests').mkdir(exist_ok=True)
    with (STATE/'run.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('A run is already active')
        w.write_json(STATE/'active.json',{'stage':'parallel-daily','started':w.now(),'run_id':run_id})
        outcomes=[];coverage_files=[]
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=discovery_limit) as pool:
                futures={pool.submit(_run_agent,w,'discovery',f'{run_id}-{shard}',_discovery_prompt(run_id,shard,str((STATE/'runs'/f'{run_id}-{shard}-coverage.json').relative_to(ROOT))),min(540,max(60,max_seconds-360))):shard for shard in SHARDS}
                for future in concurrent.futures.as_completed(futures):
                    shard=futures[future];outcomes.append(future.result());coverage_files.append(STATE/'runs'/f'{run_id}-{shard}-coverage.json')
            # Resume PDF work can scale independently; live form work remains one at a time.
            while time.monotonic()-started<max_seconds-120:
                with concurrent.futures.ThreadPoolExecutor(max_workers=resume_limit) as pool:
                    results=list(pool.map(lambda slot:_resume_one(w,run_id,slot,min(360,max(30,max_seconds-(time.monotonic()-started)-90))),range(1,resume_limit+1)))
                outcomes.extend(r for r in results if r)
                if not any(results):break
            number=0
            while time.monotonic()-started<max_seconds-30:
                number+=1
                result=_form_one(w,run_id,number,min(480,max(30,max_seconds-(time.monotonic()-started)-20)))
                if not result:break
                outcomes.append(result)
            from tracker_views import views
            views(w)
            coverage=[]
            for file in coverage_files:
                if file.is_file():coverage.append(w.read_json(file,{}))
            searches=sum(sum(bool(s.get('page_url') and s.get('checked_at') and s.get('status')) for s in c.get('searches',[])) for c in coverage)
            discovered={(str(r.get('company')),str(r.get('requisition_id'))) for c in coverage for r in c.get('original_requisitions',[]) if r.get('requisition_id')}
            saved={(w.employer(p.get('company')),w.normalize(p.get('job_id'))) for file in (ROOT/'jobs').glob('*/listing.json') if (p:=w.read_json(file,{})).get('original_verified') and p.get('live')}
            verified={(w.employer(r.get('company')),w.normalize(r.get('requisition_id'))) for c in coverage for r in c.get('original_requisitions',[]) if r.get('live_verified') is True and r.get('requisition_id')}
            verified_saved=verified & saved
            prepared=sum((ROOT/'jobs'/o['slug']/'application_draft.json').is_file() for o in outcomes if o.get('kind')=='form' and o.get('slug'))
            metrics={'search_pages_recorded':searches,'jobs_discovered_with_original_id':len(discovered),'unique_original_requisitions_verified':len(verified),'verified_jds_saved':len(verified_saved),
                     'resumes_ready':sum(o.get('stage')=='resume_ready' for o in outcomes),
                     'forms_prepared':prepared,
                     'submitted':sum(o.get('stage')=='submitted' for o in outcomes),
                     'blocked':sum(o.get('stage')=='blocked' for o in outcomes),
                     'worker_seconds':{o['name']:o['seconds'] for o in outcomes if 'name' in o},
                     'job_stage_seconds':{r['slug']:json.loads(r['stage_times']) for r in queue.status()['jobs']},
                     'queue':queue.status()['counts']}
            digest=STATE/'digests'/f'{run_id}.md'
            digest.write_text('# Parallel daily run '+run_id+'\n\n'+json.dumps({'metrics':metrics,'coverage_files':[str(p.relative_to(ROOT)) for p in coverage_files if p.is_file()],'workers':outcomes},indent=2)+'\n')
            result={'status':'Completed with measured coverage' if len(coverage)==3 else 'Partial coverage','run_id':run_id,'metrics':metrics,'digest':str(digest.relative_to(ROOT)),'worker_logs':[o['log'] for o in outcomes if 'log' in o],'elapsed_seconds':round(time.monotonic()-started,2)}
            w.write_json(STATE/'last-daily-run.json',result);w.event('parallel_daily_finished',**result)
            return result
        finally:w.write_json(STATE/'active.json',{})
