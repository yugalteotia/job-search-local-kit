"""Deterministic read-only scheduled sweep against an allowlisted config."""
import concurrent.futures as futures
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import time
import uuid
from urllib.parse import urlsplit

import browser_portals
import job_config
import job_db
import job_views
import source_adapters
import review_candidates
import lead_resolution
import portal_native
import freshness
import urgent_alerts
from local_paths import ROOT

STATE=ROOT/'.workflow'

def _say(message):
    """Progress sink; `run(progress=...)` replaces it (used by `workflow apply-now`). Never affects behavior."""
    try:
        import progress;progress.emit(message)
    except Exception:pass
    if PROGRESS is not None:
        try:PROGRESS(message)
        except Exception:pass
PROGRESS=None

def _write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,indent=2,ensure_ascii=False,default=str)+'\n')

def _run_source(section,name,record,config,run_number,page_cap,query_cap,deadline):
    from portals import registry
    return section,registry.discover(section,name,record,config,run_number=run_number,page_cap=page_cap,
                                     query_cap=query_cap,deadline=deadline)

def _coverage(db,run_id,section,result):
    pages=result.get('pages',[]) or [{'status':result['status'],'checked_at':result['checked_at'],
                                    'error_kind':result.get('error_kind'),'page':'source_summary'}]
    for page in pages:
        key=f"{section}:{page.get('query','')}:{page.get('location','')}:{page.get('page','')}:{page.get('url','')}"
        db.execute('''INSERT OR REPLACE INTO source_coverage
          (run_id,source,page_key,checked_at,status,lead_count,verified_count,error_kind)
          VALUES(?,?,?,?,?,?,?,?)''',
          (run_id,result['source'],key,page.get('checked_at') or result['checked_at'],page['status'],
           page.get('lead_count',page.get('items',0)),0,page.get('error_kind')))

def record_portal_cards(db,name,response):
    """Persist exact card IDs even when a portal hides the job-specific URL."""
    stamp=response.get('checked_at') or job_db.now()
    for lead in response.get('leads',[]):
        card_id=str(lead.get('portal_job_id') or '')
        if not card_id:continue
        db.execute('''INSERT INTO portal_card_leads(source,portal_job_id,search_page_url,canonical_url,title,status,failure_reason,first_seen,last_seen)
          VALUES(?,?,?,?,?,'ExactURL',NULL,?,?) ON CONFLICT(source,portal_job_id) DO UPDATE SET
          canonical_url=excluded.canonical_url,title=excluded.title,search_page_url=excluded.search_page_url,
          status='ExactURL',failure_reason=NULL,last_seen=excluded.last_seen''',
          (name,card_id,lead.get('search_page_url') or '',lead.get('url'),lead.get('title'),stamp,stamp))
    for page in response.get('pages',[]):
        for card in page.get('unresolved_cards',[]):
            card_id=str(card.get('portal_job_id') or '')
            if not card_id:continue
            db.execute('''INSERT INTO portal_card_leads(source,portal_job_id,search_page_url,canonical_url,title,status,failure_reason,first_seen,last_seen)
              VALUES(?,?,?,NULL,NULL,'NeedsExactURL',?,?,?) ON CONFLICT(source,portal_job_id) DO UPDATE SET
              last_seen=excluded.last_seen,failure_reason=CASE WHEN portal_card_leads.canonical_url IS NULL
              THEN excluded.failure_reason ELSE NULL END''',
              (name,card_id,page.get('url') or '',card.get('reason') or 'job_specific_url_not_visible',stamp,stamp))

def run(*,test=False,scheduled=False,max_seconds=1800,page_cap=None,query_cap=None,
        sources=None,review_limit=None,fresh_only=False,progress=None,manual=False,application_job_id=None,queue_only=False,session_check_only=False):
    global PROGRESS
    PROGRESS=progress
    config=job_config.load()
    if application_job_id is not None and (not manual or type(application_job_id) is not int or application_job_id<1):
        raise ValueError('An exact application job ID is a positive manual-run scope only')
    # Each sweep reviews its own cards, never the accumulated portal backlog.
    fresh_only=True
    control_path=STATE/'control.json'
    control=json.loads(control_path.read_text()) if control_path.is_file() else {}
    if control.get('paused'):return {'status':'Paused','reason':'Workflow pause control is active'}
    if scheduled and not manual and not config['schedule']['enabled']:
        return {'status':'Disabled','reason':'Schedule disabled in config'}
    if sources is not None and (not sources or any(name not in config['sources'] or not config['sources'][name]['enabled'] for name in sources)):
        raise ValueError('Test source scope must name enabled configured portals')
    if review_limit is not None and not 1<=review_limit<=config['review']['max_jds_per_run']:
        raise ValueError('Review limit is outside configured bounds')
    max_seconds=min(max_seconds,config['schedule']['max_seconds'])
    if max_seconds<30:raise ValueError('Run bound must be at least 30 seconds')
    lock=STATE/'daily-run.lock';lock.parent.mkdir(parents=True,exist_ok=True)
    with lock.open('a') as handle:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'OverlapPrevented','reason':'Another scheduled/manual run owns the lock'}
        import progress as progress_log
        previous_run_id=os.environ.get('JOB_SEARCH_RUN_ID')
        try:
            outcome=_locked(config,test,scheduled,max_seconds,page_cap,query_cap,sources,review_limit,fresh_only,
                            allow_live=scheduled and any(job_config.live_provider_enabled(config,p,test_mode=test,timer=not manual) for p in ('naukri','linkedin')),application_job_id=application_job_id,manual=manual,queue_only=queue_only,session_check_only=session_check_only)
        except BaseException as exc:
            # An interrupted submitting action remains locked. Reconcile only
            # after normal exception cleanup; never reopen its form.
            current_run_id=os.environ.get('JOB_SEARCH_RUN_ID')
            if isinstance(exc,Exception) and not session_check_only and current_run_id and current_run_id!=previous_run_id:
                try:
                    import outcome_reconcile
                    cleanup_db=job_db.connect()
                    try:
                        outcome_reconcile.run(cleanup_db,config,phase='after_crash',run_id=current_run_id,
                                              test_mode=test,sources=sources,application_job_id=application_job_id)
                    finally:cleanup_db.close()
                except Exception:pass  # Before the next run, the same pending locks are checked again.
            progress_log.finish('Crashed');raise
        progress_log.finish(outcome.get('status'),{'errors':len(outcome.get('errors',[])),
            'worker':(outcome.get('naukri_application_worker') or {}).get('status')})
        return outcome

def _locked(config,test,scheduled,max_seconds,page_cap,query_cap,sources,review_limit,fresh_only,allow_live=False,application_job_id=None,manual=False,queue_only=False,session_check_only=False):
    started=time.monotonic();run_id=str(uuid.uuid4());stamp=job_db.now()
    os.environ['JOB_SEARCH_RUN_ID']=run_id
    result={'run_id':run_id,'started_at':stamp,'mode':'native_portals_live' if allow_live else 'read_only','upload_enabled':allow_live,'submission_enabled':allow_live,'scheduled':scheduled,'test':test,
            'source_scope':list(sources) if sources else None,'fresh_only':fresh_only,'queue_only':queue_only,
            'continuation_of':os.environ.get('JOB_SEARCH_CONTINUATION_OF_RUN_ID'),
            'status':'Running','sources':{},'coverage':{},'leads':0,'unique_original_requisitions_verified':0,
            'fit_decisions':{},'resumes':[],'forms':[],'submissions':[],'blockers':[],'errors':[],
            'evidence_paths':[],'queue':{},'stage_seconds':{},'stage_counts':{},'top_reviewed':[]}
    db=job_db.connect();job_db.initialize(db)
    db.execute('INSERT INTO runs(id,started_at,mode,status) VALUES(?,?,?,?)',(run_id,stamp,result['mode'],'Running'))
    import progress,recovery
    trigger=os.environ.get('JOB_SEARCH_TRIGGER','timer' if scheduled else 'cli')
    db.execute('UPDATE runs SET trigger=? WHERE id=?',(trigger,run_id))
    progress.start(run_id,trigger=trigger,dry_run=test)
    if session_check_only:
        import linkedin_discovery
        health=linkedin_discovery.fetch(config['sources']['linkedin'],config['search'],session_check_only=True)
        result.update(mode='read_only',upload_enabled=False,submission_enabled=False,session_check_only=True,
                      sources={'linkedin':health},status=health['status'],finished_at=job_db.now())
        path=STATE/'runs'/f'{run_id}.json';result['evidence_paths'].append(str(path.relative_to(ROOT)))
        _write(path,result)
        db.execute('UPDATE runs SET finished_at=?,status=?,result_json=? WHERE id=?',
                   (result['finished_at'],result['status'],json.dumps(result),run_id))
        db.close();return result
    try:result['recovery']=recovery.recover_jobs(db)
    except Exception as exc:result['errors'].append({'stage':'recovery','reason':type(exc).__name__})
    import outcome_reconcile
    def reconcile_phase(phase):
        _say('Read-only application reconciliation: '+phase)
        try:
            report=outcome_reconcile.run(db,config,phase=phase,run_id=run_id,test_mode=test,
                                       sources=sources,application_job_id=application_job_id)
            result.setdefault('application_reconciliation',{})[phase]=report
            if report.get('evidence_path'):result['evidence_paths'].append(report['evidence_path'])
            for provider,summary in report.get('providers',{}).items():
                if summary.get('status')=='Blocked':
                    result['blockers'].append({'source':provider,'reason':summary.get('history',{}).get('blocker') or summary.get('error_type','history_reconciliation_unavailable'),'stage':'reconciliation_'+phase})
            _say('Reconciled existing applications: '+str(len(report.get('confirmed',[]))))
        except Exception as exc:
            result['errors'].append({'stage':'reconciliation_'+phase,'reason':type(exc).__name__})
    reconcile_phase('before')
    tasks=[(section,name,record) for section in ('sources','employer_boards')
           for name,record in config[section].items() if record['enabled'] and (sources is None or section=='sources' and name in sources)]
    if application_job_id is not None or queue_only:
        tasks=[]  # Exact staged-job continuation verifies the live listing in its worker.
    # The same lock and worker limits apply to manual and timer invocation.
    fetch_start=time.monotonic();responses=[]
    with futures.ThreadPoolExecutor(max_workers=config['concurrency']['discovery']) as pool:
        active={pool.submit(_run_source,section,name,record,config,int(dt.datetime.now(dt.timezone.utc).timestamp())//14400,page_cap,query_cap,started+max_seconds-25):(section,name)
                for section,name,record in tasks}
        try:
            for future in futures.as_completed(active,timeout=max_seconds):
                section,name=active[future]
                try:section,response=future.result()
                except Exception as exc:
                    response={'source':name,'status':'Failed','pages':[],'jobs':[],'leads':[],
                              'error_kind':type(exc).__name__,'checked_at':job_db.now(),'fetch_succeeded':False}
                responses.append((section,response))
        except futures.TimeoutError:result['errors'].append('discovery_deadline_exceeded')
    result['stage_seconds']['discovery']=round(time.monotonic()-fetch_start,2)
    _say('Sources checked: '+', '.join(f"{r['source']}={r.get('status')}" for _,r in responses))
    originals=[]
    portal_unique=set()
    for section,response in responses:
        name=response['source'];result['sources'][name]={k:response.get(k) for k in ('status','error_kind','checked_at')}
        result['sources'][name].update({'pages_checked':len(response.get('pages',[])),
                                        'leads':len(response.get('leads',[])),
                                        'feed_jobs':len(response.get('jobs',[]))})
        result['coverage'][name]=response.get('pages',[])
        result['leads']+=len(response.get('leads',[]))+len(response.get('jobs',[]))
        _coverage(db,run_id,section,response)
        if section=='sources':record_portal_cards(db,name,response)
        if response.get('status') in ('Blocked','Failed','BlankPage','NoVisibleCards','CardsWithoutExactLinks'):
            result['blockers'].append({'source':name,'reason':response.get('error_kind') or response['status']})
        if not test and response.get('fetch_succeeded') and response.get('status')=='Success':
            job_config.note_success(section,name,response['checked_at'])
        if section=='employer_boards':originals.extend(response.get('jobs',[]))
        else:
            for lead in response.get('leads',[]):
                url=lead.get('url');title=lead.get('title')
                parsed=urlsplit(str(url or ''))
                ats_allowed=lead_resolution.provider_for_url(url,config['direct_requisition_rules']) is not None
                linked_foundit=(name=='foundit' and lead.get('provenance')=='selected_card_visible_employer_link'
                    and str(lead.get('portal_job_id') or '').isdigit() and
                    lead_resolution.classify(name,url)=='NeedsOriginal')
                if parsed.scheme!='https' or parsed.username or parsed.password or not title or (parsed.hostname!=config['sources'][name]['host'] and not ats_allowed and not linked_foundit):continue
                portal_unique.add(url)
                queue_source='linkedin' if linked_foundit and parsed.hostname=='www.linkedin.com' else name
                db.execute("INSERT INTO lead_queue(canonical_url,source,title,first_seen,last_seen,state) VALUES(?,?,?,?,?,?) ON CONFLICT(canonical_url) DO UPDATE SET last_seen=excluded.last_seen",
                           (url,queue_source,title,response['checked_at'],response['checked_at'],lead_resolution.classify(queue_source,url)))
                portal_id=str(lead.get('portal_job_id') or '')
                if portal_id and lead.get('search_page_url'):
                    db.execute("INSERT INTO lead_provenance(canonical_url,source,portal_job_id,search_page_url,method,first_seen,last_seen) VALUES(?,?,?,?,?,?,?) ON CONFLICT(canonical_url,source,portal_job_id) DO UPDATE SET last_seen=excluded.last_seen,search_page_url=excluded.search_page_url",
                               (url,name,portal_id,lead['search_page_url'],lead.get('provenance') or 'visible_search_card_link',
                                response['checked_at'],response['checked_at']))
    # Rank normalized postings before bounded original-page verification. Feed counts
    # are not original-JD or fit-review counts.
    grouped={}
    for job in originals:
        board=config['employer_boards'].get(job['source'],{})
        category,reason=freshness.classify(job.get('posted_at'),board.get('last_successful_fetch'),config['freshness'])
        prior=db.execute('SELECT state,jd_text FROM jobs WHERE canonical_url=?',(job['canonical_url'],)).fetchone()
        if prior and prior['state'] not in ('Shortlisted','ResumeReady','Preparing','Prepared','NeedsAnswer','SubmissionUnconfirmed') and prior['jd_text']==job.get('jd_text'):
            result.setdefault('ats_freshness',{'repeat_skipped':0,'skipped_old':0,'unknown_date':0,'fresh':0,'recent':0})['repeat_skipped']+=1
            continue
        if category=='Old':
            result.setdefault('ats_freshness',{'repeat_skipped':0,'skipped_old':0,'unknown_date':0,'fresh':0,'recent':0})['skipped_old']+=1
            continue
        result.setdefault('ats_freshness',{'repeat_skipped':0,'skipped_old':0,'unknown_date':0,'fresh':0,'recent':0})[
            'unknown_date' if category=='Unknown' else category.lower()]+=1
        if review_candidates.INDIA.search(job.get('location') or '') and (
            review_candidates.ENGINEER.search(job.get('title') or '') or
            (review_candidates.JAVA.search(job.get('jd_text') or '') and review_candidates.SPRING.search(job.get('jd_text') or ''))):
            grouped.setdefault(job['employer'],[]).append(job)
    selected=[]
    for employer,items in grouped.items():
        selected.extend(sorted(items,key=review_candidates.priority,reverse=True)[:config['review']['max_original_checks_per_company']])
    result['stage_counts']['normalized_unique_ats']=len({job['canonical_url'] for job in originals})
    result['stage_counts']['selected_for_original_check']=len(selected)
    # Verify selected jobs at their exact allowlisted original hosted URLs, concurrently.
    verify_start=time.monotonic();verified=[]
    with futures.ThreadPoolExecutor(max_workers=config['concurrency']['verification']) as pool:
        attempts={pool.submit(source_adapters.verify_original,job):job for job in selected}
        try:
            for future in futures.as_completed(attempts,timeout=max(1,max_seconds-(time.monotonic()-started))):
                job=attempts[future]
                try:check=future.result()
                except Exception as exc:check={'verified':False,'error_kind':type(exc).__name__,'checked_at':job_db.now()}
                if not check['verified']:
                    result['errors'].append({'original_url':job['canonical_url'],'reason':check['error_kind']});continue
                job['checked_at']=check['checked_at'];verified.append(job)
        except futures.TimeoutError:result['errors'].append('original_verification_deadline_exceeded')
    result['stage_seconds']['original_verification']=round(time.monotonic()-verify_start,2)
    _say(f"Original employer pages verified: {len(verified)} of {len(selected)} selected")
    seen=set();verified_by_source={};verified_ids=[]
    for job in verified:
        try:
            identity=job_db.identity(job['employer'],job.get('requisition_id'),job['canonical_url'])
            if identity in seen:continue
            seen.add(identity)
            job_id,created=job_db.create_job(db,job)
            verified_ids.append(job_id)
            if created:
                job_db.transition(db,job_id,'Verified','original_verifier','Original hosted ATS page verified',job['canonical_url'],'Discovered')
                # Fit remains conservative until an evidence packet cites every essential item.
                result['fit_decisions'][str(job_id)]={'fit':'Unrated','eligible':False,'reason':'Exact JD requirement evidence pending'}
            result['unique_original_requisitions_verified']+=1
            verified_by_source[job['source']]=verified_by_source.get(job['source'],0)+1
        except ValueError as exc:
            if 'Historical requisition' in str(exc):continue
            result['errors'].append({'original_url':job['canonical_url'],'reason':str(exc)})
    for name,count in verified_by_source.items():
        result['sources'][name]['unique_originals_verified']=count
        db.execute('''UPDATE source_coverage SET verified_count=? WHERE run_id=? AND source=? AND page_key=(
          SELECT page_key FROM source_coverage WHERE run_id=? AND source=? ORDER BY page_key LIMIT 1)''',
          (count,run_id,name,run_id,name))
    lead_start=time.monotonic()
    raw_leads=[(response['source'],lead) for section,response in responses if section=='sources'
                 for lead in response.get('leads',[]) if lead.get('url')]
    by_source={}
    for source,lead in raw_leads:by_source.setdefault(source,[]).append(lead)
    for items in by_source.values():
        items.sort(key=lambda lead:lead_resolution.lead_priority({'title':lead.get('title'),
            'first_seen':stamp,'last_seen':stamp}),reverse=True)
    balanced=[]
    while any(by_source.values()):
        for source in sorted(by_source):
            if by_source[source]:balanced.append((source,by_source[source].pop(0)))
    fresh_leads,fresh_counts,fresh_reasons=freshness.select(db,balanced,config)
    result['freshness']={'policy':config['freshness'],'counts':fresh_counts,
        'detail_candidates':len(fresh_leads),'skips':fresh_reasons}
    native_leads=[(source,lead) for source,lead in fresh_leads if source in ('naukri','linkedin','cutshort')
                  and urlsplit(lead['url']).hostname==config['sources'][source]['host']]
    result['portal_native']=portal_native.verify_leads(db,native_leads,
        limit=min(30,max(15,(review_limit or config['review']['max_jds_per_run'])*2)),
        per_company=config['review']['per_company_cap'])
    outbound=[lead['url'] for source,lead in fresh_leads if source=='foundit'
              and urlsplit(lead['url']).hostname not in ('www.foundit.in','www.linkedin.com')]
    result['lead_resolution']=lead_resolution.resolve(db,config,
        limit=config['review']['max_portal_leads_per_run'],
        target_urls=outbound if fresh_only else None,recheck_ats=not fresh_only)
    linked_ids=[lead_resolution._portal_job_id(lead['url']) for source,lead in fresh_leads if source=='foundit'
                and urlsplit(lead['url']).hostname=='www.linkedin.com']
    linked_ids=[x for x in linked_ids if x]
    result['linked_portal_native']=lead_resolution.review_portal_native(db,config,linked_ids,
        limit=min(len(linked_ids),review_limit or config['review']['max_jds_per_run'])) if linked_ids else None
    result['stage_seconds']['lead_resolution']=round(time.monotonic()-lead_start,2)
    review_start=time.monotonic()
    resolved_ids=[check.get('job_id') for item in result['lead_resolution']['results']
                  for check in item.get('direct_checks',[]) if check.get('job_id')]
    fresh_ids=list(dict.fromkeys(verified_ids+result['portal_native']['verified_job_ids']+[x for x in resolved_ids if x]))
    known_ids=fresh_ids if fresh_only else [row['id'] for row in db.execute("SELECT id FROM jobs WHERE state='Verified' AND jd_text IS NOT NULL")]
    queued_review=review_candidates.enqueue(db,known_ids)
    linked_reviewed=result['linked_portal_native']['review_results'] if result['linked_portal_native'] else []
    reviewed=linked_reviewed+review_candidates.consume(db,config,
        limit=max(0,(review_limit or config['review']['max_jds_per_run'])-len(linked_reviewed)),
        per_company=config['review']['per_company_cap'],allowed_job_ids=fresh_ids if fresh_only else None)
    result['stage_seconds']['fit_review']=round(time.monotonic()-review_start,2)
    _say(f'Roles reviewed against JD evidence: {len(reviewed)}')
    result['fit_decisions'].update({str(item['job_id']):{k:item[k] for k in ('fit','eligible_for_resume','reasons','evidence','jd_sha256')}
                                    for item in reviewed})
    result['top_reviewed']=reviewed[:15]
    result['stage_counts'].update({'fetched':result['leads'],'unique_portal_leads':len(portal_unique),
                            'fresh':fresh_counts['fresh']+result.get('ats_freshness',{}).get('fresh',0),
                            'unknown_date':fresh_counts['unknown_date']+result.get('ats_freshness',{}).get('unknown_date',0),
                            'skipped_old':fresh_counts['skipped_old']+result.get('ats_freshness',{}).get('skipped_old',0),
                            'repeat_skipped':fresh_counts['repeat_skipped']+result.get('ats_freshness',{}).get('repeat_skipped',0),
                            'unique_original_requisitions':len(seen),
                            'eligible_for_review':sum(review_candidates.eligible(db.execute('SELECT * FROM jobs WHERE id=?',(item,)).fetchone()) for item in fresh_ids),
                            'original_jds_reviewed':sum(not str(db.execute('SELECT source FROM jobs WHERE id=?',(x['job_id'],)).fetchone()['source']).endswith('_portal_native') for x in reviewed),
                            'original_or_portal_jds_reviewed':len(reviewed),
                            'portal_native_verified':result['portal_native']['verified']+(result['linked_portal_native']['portal_native_verified'] if result['linked_portal_native'] else 0),
                            'high':sum(x['fit']=='High' for x in reviewed),
                            'queued_for_resume':db.execute("SELECT count(*) FROM queue WHERE state='QueuedResume'").fetchone()[0],
                            'review_queue_added':queued_review,
                            'portal_originals_matched':result['lead_resolution']['originals_matched'],
                            'portal_leads_still_needing_original':result['lead_resolution']['needs_original']})
    result['review_coverage']={'by_source':{},'by_company':{}}
    for item in reviewed:
        source=db.execute('SELECT source FROM jobs WHERE id=?',(item['job_id'],)).fetchone()['source']
        result['review_coverage']['by_source'][source]=result['review_coverage']['by_source'].get(source,0)+1
        result['review_coverage']['by_company'][item['company']]=result['review_coverage']['by_company'].get(item['company'],0)+1
    try:
        import master_check
        check=master_check.check();result['master_resume']={'ok':check['ok'],'issues':[i['kind'] for i in check['issues']]}
    except Exception as exc:result['master_resume']={'ok':None,'error':type(exc).__name__}
    # Fit -> resume hand-off for the Naukri lane: deterministic, evidence-checked, no browser.
    if config['naukri_worker']['enabled']:
        stage_start=time.monotonic()
        try:
            import auto_fit,auto_resume
            result['auto_fit']=auto_fit.run(db,config)
            result['auto_resume']=auto_resume.run(db,config)
        except Exception as exc:
            result['errors'].append({'stage':'auto_fit_resume','reason':type(exc).__name__,'detail':str(exc)[:200]})
        result['stage_seconds']['auto_fit_resume']=round(time.monotonic()-stage_start,2)
        _say(f"Fit review: {(result.get('auto_fit') or {}).get('queued',0)} queued; resumes prepared: {(result.get('auto_resume') or {}).get('prepared',0)}")
    queue_counts={row['state']:row['n'] for row in db.execute('SELECT state,count(*) n FROM queue GROUP BY state')}
    result['queue']=queue_counts
    import naukri_native_worker as queue_worker
    result['queue_eligibility']={
        'naukri_ready_candidates':len(queue_worker.candidates(db,config)),
        'resume_jobs_ready_to_build':db.execute("SELECT count(*) FROM queue q JOIN jobs j ON j.id=q.job_id WHERE q.state='QueuedResume' AND j.state='Shortlisted' AND j.source='naukri_portal_native' AND NOT EXISTS(SELECT 1 FROM submission_attempts a WHERE a.job_id=j.id)").fetchone()[0],
        'locked_or_applied_queue_rows':db.execute("SELECT count(*) FROM queue q JOIN jobs j ON j.id=q.job_id WHERE j.state IN ('SubmissionUnconfirmed','Applied')").fetchone()[0]}
    # Stage exact, unapplied requisitions for the next guarded pilot. This is
    # metadata only: the discovery timer cannot upload or submit.
    ready=[dict(row) for row in db.execute("""SELECT j.id AS job_id,j.employer,j.title,j.requisition_id,
        j.canonical_url,j.fit,q.priority FROM queue q JOIN jobs j ON j.id=q.job_id
        WHERE q.state='QueuedResume' AND j.state='Shortlisted'
        AND (j.fit='High' OR (j.source='naukri_portal_native' AND j.fit='Medium' AND json_extract(j.fit_json,'$.result.strong_medium_eligible')=1))
        AND NOT EXISTS(SELECT 1 FROM submission_attempts a WHERE a.job_id=j.id)
        ORDER BY q.priority DESC,q.queued_at LIMIT 10""")]
    pending=[dict(row) for row in db.execute("""SELECT j.id AS job_id,j.employer,j.title,j.requisition_id,
        j.canonical_url,q.priority FROM review_queue q JOIN jobs j ON j.id=q.job_id
        WHERE q.state='Pending' AND j.state='Verified'
        AND NOT EXISTS(SELECT 1 FROM submission_attempts a WHERE a.job_id=j.id)
        ORDER BY q.priority DESC,q.queued_at LIMIT 10""")]
    staging={'run_id':run_id,'updated_at':job_db.now(),'resume_review_ready':ready,
             'needs_jd_evidence_review':pending,'uploads_enabled':False,'submissions_enabled':False}
    _write(STATE/'staging.json',staging)
    result['staging']={'path':str((STATE/'staging.json').relative_to(ROOT)),
                       'resume_review_ready':len(ready),'needs_jd_evidence_review':len(pending)}
    try:result['blocker_alerts']=urgent_alerts.sync_sqlite_blockers(db,config)
    except Exception as exc:result['errors'].append({'stage':'blocker_alerts','reason':type(exc).__name__})
    # Application work uses independent Naukri and LinkedIn lanes. Test mode runs only
    # candidate/input checks; it cannot launch Chrome, change the profile, or
    # send questionnaire answers.
    result['naukri_application_worker']={'enabled':bool(config['naukri_worker']['enabled']),
        'max_submissions_per_run':config['naukri_worker']['max_submissions_per_run'],
        'max_new_attempts_per_run':config['portal_workers']['naukri']['max_submissions_per_run'],
        'other_portals':'outside_focus_disabled'}
    from portals import registry
    if allow_live:
        import naukri_native_worker
        result['profile_restore_recovery']=naukri_native_worker.recover_pending_restore()
    try:result['portals']=registry.capabilities(db,config)
    except Exception as exc:result['errors'].append({'stage':'capabilities','reason':type(exc).__name__})
    target=db.execute('SELECT source FROM jobs WHERE id=?',(application_job_id,)).fetchone() if application_job_id else None
    for provider in ('naukri','linkedin'):
        key=provider+'_application_worker';limit=config['portal_workers'][provider].get('max_submissions_per_run',1)
        result.setdefault(key,{'enabled':config['portal_workers'][provider]['apply_enabled'],'max_submissions_per_run':limit,'max_new_attempts_per_run':limit})
        scoped=bool(target and target['source']==provider+'_portal_native')
        lane,reason=registry.apply_lane_worker(db,config,provider,allow_bootstrap=bool(manual and scoped))
        result[key]['capability_reason']=reason
        live=job_config.live_provider_enabled(config,provider,test_mode=test,timer=not manual)
        if scheduled and lane is not None and (test or live) and (sources is None or provider in sources) and (not application_job_id or scoped):
            _say(provider+' worker: '+('dry run' if test else 'live, max '+str(limit)))
            try:result[key].update(lane.apply_lane(db,config,test_mode=test,max_submissions=limit,application_job_id=application_job_id))
            except Exception as exc:
                result[key].update(status='Blocked',error_type=type(exc).__name__)
                result['errors'].append({'stage':key,'reason':type(exc).__name__})
        else:result[key].update(status='DiscoveryOnly',candidate_count=0,submissions=0)
        _say(provider+' worker finished: '+str(result[key]['status']))
        if not test:
            for item in result[key].get('results',[]):
                if item.get('status')=='Applied' and item.get('confirmation_path'):
                    outcome_reconcile.queue_notification(db,item['job_id'],item.get('attempt_id'),item['confirmation_path'])
    reconcile_phase('after')
    result['blocker_alerts'].extend(urgent_alerts.sync_sqlite_blockers(db,config))
    # The proxy records exact created IDs and automatic closes. Helper events
    # add navigations that reused the worker's already-owned tab.
    proxy_log=STATE/'runs'/f'{run_id}-tabs.jsonl'
    helper_log=STATE/'runs'/f'{run_id}-tabs-helper.jsonl'
    def read_events(path):
        events=[]
        if path.exists():
            for line in path.read_text(encoding='utf-8').splitlines():
                try:events.append(json.loads(line))
                except ValueError:continue
        return events
    proxy_events=read_events(proxy_log);helper_events=read_events(helper_log)
    proxy_summaries=[x for x in proxy_events if x.get('event')=='tab_lifecycle_summary']
    helper_summaries=[x for x in helper_events if x.get('event')=='tab_lifecycle_summary']
    source_summaries=proxy_summaries or helper_summaries
    if source_summaries:
        result['tab_lifecycle']={key:sum(int(x.get(key,0) or 0) for x in source_summaries)
                                 for key in ('created','closed','remaining_automation_tabs','job_tabs_created',
                                             'job_tabs_closed','welcome_tabs_created','welcome_tabs_closed',
                                             'remaining_job_tabs','remaining_welcome_tabs')}
        result['tab_lifecycle']['reused']=max(
            sum(int(x.get('reused',0) or 0) for x in proxy_summaries),
            sum(int(x.get('reused',0) or 0) for x in helper_summaries))
        result['tab_lifecycle']['uncertain_ids']=[v for x in source_summaries for v in x.get('remaining_tab_ids',[])]
        result['tab_lifecycle']['recorded_ids']=(
            [{'id':x.get('tab_id'),'kind':'job'} for x in proxy_events if x.get('event')=='tab_created']+
            [{'id':x.get('tab_id'),'kind':'welcome'} for x in proxy_events if x.get('event')=='welcome_tab_created'])
        result['tab_lifecycle']['log_paths']=[str(p.relative_to(ROOT)) for p in (proxy_log,helper_log) if p.exists()]
    else:
        result['tab_lifecycle']={'created':None,'reused':None,'closed':None,'remaining_automation_tabs':None,
                                 'job_tabs_created':None,'job_tabs_closed':None,'welcome_tabs_created':None,
                                 'welcome_tabs_closed':None,'remaining_job_tabs':None,'remaining_welcome_tabs':None,
                                 'uncertain_ids':['No tab lifecycle report was emitted'],'recorded_ids':[],
                                 'log_paths':[]}
    result['status']='Partial' if result['errors'] or any(x['status']!='Success' for x in result['sources'].values()) else 'Success'
    result['finished_at']=job_db.now();result['elapsed_seconds']=round(time.monotonic()-started,2)
    path=STATE/'runs'/f'{run_id}.json';result['evidence_paths'].append(str(path.relative_to(ROOT)))
    _write(path,result)
    db.execute('UPDATE runs SET finished_at=?,status=?,result_json=? WHERE id=?',
               (result['finished_at'],result['status'],json.dumps(result),run_id))
    db.close()
    result['views']=job_views.regenerate()
    _write(STATE/'last-daily-run.json',{'run_id':run_id,'status':result['status'],'log':str(path.relative_to(ROOT)),
                                         'finished_at':result['finished_at']})
    return result
