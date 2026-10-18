"""Reject navigation links and inspect exact portal jobs for allowlisted originals."""
import concurrent.futures as futures
import datetime as dt
import json
import re
from urllib.parse import urlsplit,parse_qs

from parallel_browser_probe import MCP
from source_adapters import ORIGINAL_HOSTS,verify_direct_requisition,verify_linked_employer_requisition
import job_db
from tab_lifecycle import OwnedTabs

ATS_HOSTS=set().union(*ORIGINAL_HOSTS.values())
JAVA=re.compile(r'\bjava\b',re.I)
SPRING=re.compile(r'spring\s*boot|springboot',re.I)
BACKEND=re.compile(r'backend|back.end|microservices?|\bapi\b',re.I)

def lead_priority(row):
    title=re.sub(r'\s+with verification$','',row['title'] or '',flags=re.I).splitlines()[0]
    score=0
    score+=45 if JAVA.search(title) else 0
    score+=40 if SPRING.search(title) else 0
    score+=35 if BACKEND.search(title) else 0
    score+=15 if re.search(r'engineer|developer|sde',title,re.I) else 0
    if re.search(r'\b(?:staff|principal|manager|director|intern|fresher)\b',title,re.I):score-=45
    years=re.search(r'\b(\d+(?:\.\d+)?)\s*(?:\+|[-–]\s*\d+(?:\.\d+)?)?\s*(?:years?|yrs?)\b',title,re.I)
    if years:score+=15 if float(years.group(1))<=3 else -20
    stamp=row['last_seen'] or row['first_seen']
    try:
        checked=dt.datetime.fromisoformat(stamp.replace('Z','+00:00'))
        age_hours=max(0,(dt.datetime.now(dt.timezone.utc)-checked.astimezone(dt.timezone.utc)).total_seconds()/3600)
        score+=max(0,10-age_hours/24)
    except (ValueError,TypeError):pass
    return score

def select_pending(rows,limit):
    """Pick high-signal and recent portal jobs with source interleaving."""
    by_source={}
    for row in sorted(rows,key=lambda x:(lead_priority(x),x['last_seen'] or ''),reverse=True):
        by_source.setdefault(row['source'],[]).append(row)
    ordered=[]
    while len(ordered)<limit and any(by_source.values()):
        for source in sorted((s for s in by_source if by_source[s]),key=lambda s:(lead_priority(by_source[s][0]),by_source[s][0]['last_seen'] or ''),reverse=True):
            if by_source[source] and len(ordered)<limit:ordered.append(by_source[source].pop(0))
    return ordered

def classify(source,url):
    parsed=urlsplit(url or '')
    if parsed.scheme!='https' or not parsed.hostname:return 'NotAJob'
    path=parsed.path
    if parsed.hostname in ATS_HOSTS:return 'NeedsOriginal' if len(path.strip('/').split('/'))>=2 else 'NotAJob'
    if source=='linkedin':return 'NeedsOriginal' if re.fullmatch(r'/jobs/view/\d+/?',path) else 'NotAJob'
    if source=='indeed':
        jk=parse_qs(parsed.query).get('jk',[''])[0]
        return 'NeedsOriginal' if path=='/viewjob' and re.fullmatch(r'[a-f0-9]{12,24}',jk,re.I) else 'NotAJob'
    if source=='naukri':return 'NeedsOriginal' if path.startswith('/job-listings-') and re.search(r'\d{8,}',path) else 'NotAJob'
    if source=='foundit':
        if parsed.hostname=='www.foundit.in' and re.search(r'/job/.+-\d{5,}/?$',path):return 'NeedsOriginal'
        if parsed.hostname=='www.linkedin.com' and re.fullmatch(r'/jobs/view/\d+/?',path):return 'NeedsOriginal'
        if (parsed.hostname.endswith('.myworkdayjobs.com') or parsed.hostname=='wd1.myworkdaysite.com') and re.search(r'/job/.+_[A-Z0-9-]{4,}/?$',path,re.I):
            return 'NeedsOriginal'
        return 'NotAJob'
    if source=='cutshort':return 'NeedsOriginal' if re.match(r'/job/[^/]{8,}',path) else 'NotAJob'
    return 'NotAJob'

def provider_for_url(url,rules,link_text='',source_host=None):
    parsed=urlsplit(str(url or ''))
    if parsed.scheme!='https' or parsed.username or parsed.password:return None
    for provider,rule in rules.items():
        if provider=='employer_site':continue
        if rule['enabled'] and parsed.hostname in rule['hosts'] and re.fullmatch(rule['path_pattern'],parsed.path,re.I):
            return provider
    employer_rule=rules.get('employer_site',{})
    if (employer_rule.get('enabled') and source_host and parsed.hostname!=source_host and
            re.fullmatch(employer_rule['apply_link_text_pattern'],str(link_text or ''),re.I) and
            re.search(employer_rule['path_pattern'],parsed.path,re.I)):
        return 'employer_site'
    return None

DOM=r'''() => {
  const body=document.body?.innerText||''; const i=body.indexOf('About the job');
  const text=(selector)=>document.querySelector(selector)?.innerText?.trim()||'';
  const docParts=document.title.replace(/\s*\|\s*LinkedIn\s*$/i,'').split(/\s+\|\s+/);
  const title=text('h1.job-details-jobs-unified-top-card__job-title')||text('h1')||docParts.slice(0,-1).join(' | ')||docParts[0]||'';
  const employer=text('.job-details-jobs-unified-top-card__company-name')||text('[data-test-job-card-company-name]')||(docParts.length>1?docParts.at(-1):'');
  const jobLocation=text('.job-details-jobs-unified-top-card__primary-description-container')||text('[class*=job-details-jobs-unified-top-card__primary-description]');
  const rawDescription=text('#jobDescriptionText')||text('.jobs-description__content')||text('[data-test-id*=job-description]')||text('[class*=description__text]')||(i>=0?body.slice(i,i+20000):'');
  const before=i>=0?body.slice(0,i):body.slice(0,3000);
  const titleAt=before.lastIndexOf(title);
  const headerExcerpt=titleAt>=0?before.slice(titleAt,titleAt+650):'';
  const descriptionEnd=rawDescription.search(/\n(?:About the company|Set alert for similar jobs|More jobs)\s*\n/i);
  const description=descriptionEnd>250?rawDescription.slice(0,descriptionEnd):rawDescription;
  const sameJobAlert=(body.match(/Set alert for similar jobs\s*\n+([^\n]{3,160})\s*\n+Off\b/i)||[])[1]||'';
  const external=[...document.querySelectorAll('a[href]')].map(a=>{try{const u=new URL(a.href);
    return ['jobs.lever.co','job-boards.greenhouse.io','boards.greenhouse.io','jobs.ashbyhq.com'].includes(u.hostname)
      ? {url:u.origin+u.pathname,text:(a.innerText||a.getAttribute('aria-label')||'').trim().slice(0,100)}:null}catch{return null}}).filter(Boolean).slice(0,20);
  const apply_links=[...document.querySelectorAll('a[href]')].map(a=>{try{const u=new URL(a.href);const t=(a.innerText||a.getAttribute('aria-label')||'').trim();
    return u.protocol==='https:'&&u.hostname!==location.hostname&&/^(apply(?: now| on company site)?|view (?:job|role)|company site)$/i.test(t)
      ? {url:u.origin+u.pathname+u.search,text:t.slice(0,100)}:null}catch{return null}}).filter(Boolean).slice(0,5);
  const controls=[...document.querySelectorAll('button,a[role=button],a[href]')].filter(x=>/^(easy apply|apply|apply now)$/i.test((x.innerText||x.getAttribute('aria-label')||'').trim())).map(x=>({text:(x.innerText||x.getAttribute('aria-label')||'').trim(),disabled:!!x.disabled||x.getAttribute('aria-disabled')==='true',href:x.href||null,visible:!!(x.offsetWidth||x.offsetHeight||x.getClientRects().length)})).filter(x=>x.visible).slice(0,10);
  const unavailability=/no longer accepting applications|job is closed|position (?:is )?filled|this job is no longer available/i.test(body);
  const posted=[...document.querySelectorAll('time')].map(x=>x.innerText.trim()).find(Boolean)||'';
  const employerReq=(body.match(/(?:requisition|job)\s*(?:id|number|#)\s*[:#-]?\s*([A-Z0-9-]{4,})/i)||[])[1]||null;
  return {host:location.hostname,path:location.pathname,title:document.title,job_title:title,employer,
    blocked:/access denied|captcha|verify you are human|unusual traffic|403 forbidden/i.test(body.slice(0,1800)),
    job_excerpt:description.slice(0,20000),job_header_excerpt:headerExcerpt,same_job_alert:sameJobAlert,external_origins:external,
    job_location:jobLocation.slice(0,250),posted_text:posted.slice(0,100),
    employer_requisition_id:employerReq,listing_unavailable:unavailability,application_controls:controls,
    apply_links,apply_buttons:controls.map(x=>x.text)};
}'''

def _parse(result):
    data=' '.join(x.get('text','') for x in result.get('content',[]) if x.get('type')=='text')
    for i,c in enumerate(data):
        if c!='{':continue
        try:
            value,_=json.JSONDecoder().raw_decode(data[i:])
            if isinstance(value,dict) and 'host' in value:return value
        except ValueError:pass
    raise ValueError('No structured portal job observation')

def inspect_portal(source,url):
    client=None;owned=None
    try:
        client=MCP();owned=OwnedTabs(client, owner=f"lead:{source}");owned.open(url)
        data=_parse(client.tool('browser_evaluate',{'function':DOM},timeout=30))
        if data.get('blocked'):return {'status':'Blocked','error':'site_verification_or_403'}
        if data.get('host')!=urlsplit(url).hostname or data.get('path')!=urlsplit(url).path:
            return {'status':'PortalRedirect','error':'Exact portal job path changed'}
        if 'not found' in (data.get('title') or '').lower():
            return {'status':'Unavailable','page_title':data.get('title')}
        if not data.get('job_excerpt'):
            return {'status':'InspectedTitleOnly','page_title':data.get('title'),
                    'job_title':data.get('job_title'),'employer_label':data.get('employer') or ((data.get('title') or '').split('|')[-2].strip() if '|' in (data.get('title') or '') else None),
                    'external_origins':data.get('external_origins',[]),'apply_links':data.get('apply_links',[]),
                    'location':data.get('job_location') or None,'posted_text':data.get('posted_text') or None,
                    'same_job_alert':data.get('same_job_alert') or None,
                    'job_header_excerpt':data.get('job_header_excerpt') or None,
                    'application_controls':data.get('application_controls',[]),'listing_unavailable':data.get('listing_unavailable'),
                    'employer_requisition_id':data.get('employer_requisition_id')}
        return {'status':'Inspected','page_title':data.get('title'),'job_title':data.get('job_title'),
                'employer_label':data.get('employer') or ((data.get('title') or '').split('|')[-2].strip() if '|' in (data.get('title') or '') else None),
                'jd_excerpt':data.get('job_excerpt'),'location':data.get('job_location') or None,
                'same_job_alert':data.get('same_job_alert') or None,
                'job_header_excerpt':data.get('job_header_excerpt') or None,
                'posted_text':data.get('posted_text') or None,'external_origins':data.get('external_origins',[]),
                'apply_links':data.get('apply_links',[]),
                'apply_buttons':data.get('apply_buttons',[]),'application_controls':data.get('application_controls',[]),
                'listing_unavailable':data.get('listing_unavailable'),'employer_requisition_id':data.get('employer_requisition_id')}
    except (RuntimeError,TimeoutError,ValueError,OSError) as exc:
        return {'status':'PortalError','error':type(exc).__name__}
    finally:
        if client:
            if owned:owned.close_all()
            client.close()

def recheck_missing_ats_jds(db,config,limit=7):
    """Refresh short target-title ATS records from their exact allowlisted URLs."""
    import review_candidates
    rows=db.execute("SELECT * FROM jobs WHERE availability='Live' AND jd_text IS NOT NULL ORDER BY id").fetchall()
    target=re.compile(r'java|spring|backend|back.end|microservices?|\bapi\b|software (?:development )?(?:engineer|developer)|\bsde\b',re.I)
    candidates=[r for r in rows if target.search(r['title'] or '') and review_candidates.INDIA.search(r['location'] or '')
                and not review_candidates.substantive_jd(r['jd_text'])]
    out=[]
    for row in candidates[:max(0,min(limit,10))]:
        provider=provider_for_url(row['canonical_url'],config['direct_requisition_rules'])
        if not provider:
            out.append({'job_id':row['id'],'company':row['employer'],'role':row['title'],'direct_url':row['canonical_url'],
                        'status':'Unresolved','failure_reason':'direct URL does not match an enabled ATS provider/path rule'})
            continue
        check=verify_direct_requisition(row['canonical_url'],provider)
        item={'job_id':row['id'],'company':row['employer'],'role':row['title'],'direct_url':row['canonical_url'],
              'requisition_id':row['requisition_id'],'provider':provider,**check}
        if check.get('verified'):
            # This is the same exact canonical URL already present in `row`.
            # Calling create_job() here attempts a second insert and violates
            # the unique URL index whenever employer/requisition metadata was
            # not identical. Refresh this row in place and preserve its
            # identity and any application state.
            provenance=json.loads(row['provenance_json'] or '{}')
            provenance['latest_original_verification']={
                'provider':provider,'direct_url':row['canonical_url'],
                'checked_at':check['checked_at'],'evidence':check['evidence']}
            db.execute('''UPDATE jobs SET title=?,location=coalesce(?,location),
                requisition_id=coalesce(requisition_id,?),posted_at=coalesce(?,posted_at),
                checked_at=?,jd_text=?,availability='Live',provenance_json=?,updated_at=? WHERE id=?''',
                (check['title'],check.get('location'),check.get('requisition_id'),check.get('posted_at'),
                 check['checked_at'],check['jd_text'],job_db.data_json(provenance),job_db.now(),row['id']))
            item.update({'job_id':row['id'],'updated_from_original':True,'record_created':False})
        else:
            item['updated_from_original']=False
            item['failure_reason']=check.get('error_kind') or check.get('status')
            if check.get('status')=='LiveTitleOnly':
                old=db.execute('SELECT fit,fit_json,provenance_json FROM jobs WHERE id=?',(row['id'],)).fetchone()
                prior_review=db.execute('SELECT review_json FROM review_queue WHERE job_id=?',(row['id'],)).fetchone()
                invalidation={'fit':'Unresolved','eligible_for_resume':False,'not_a_reviewed_jd':True,
                    'review_version':7,'checked_at':check['checked_at'],'exact_url':row['canonical_url'],
                    'failure_reason':check.get('error_kind'),'visible_chars':check.get('visible_chars'),
                    'invalidated_prior_fit':old['fit'] if old else None,
                    'prior_review_json':json.loads(prior_review['review_json']) if prior_review and prior_review['review_json'] else None}
                provenance=json.loads(old['provenance_json'] or '{}') if old else {}
                provenance['latest_exact_page_check']={k:check.get(k) for k in ('status','checked_at','url','error_kind','visible_chars')}
                db.execute('UPDATE jobs SET fit=\'Unrated\',fit_json=?,provenance_json=?,checked_at=?,updated_at=? WHERE id=?',
                           (job_db.data_json(invalidation),job_db.data_json(provenance),check['checked_at'],job_db.now(),row['id']))
                db.execute("UPDATE review_queue SET state='NeedsOriginalJD',reviewed_at=NULL,review_json=? WHERE job_id=?",
                           (job_db.data_json(invalidation),row['id']))
        out.append(item)
    return {'candidates_found':len(candidates),'checked':len(out),'verified_substantive':sum(bool(x.get('verified') and x.get('updated_from_original')) for x in out),
            'failed_or_unresolved':sum(not (x.get('verified') and x.get('updated_from_original')) for x in out),'results':out}

def refresh_ats_records(db,config,job_ids):
    """Refresh exact ATS posting metadata and persist dates stated on the page."""
    results=[]
    for job_id in job_ids:
        row=db.execute("SELECT * FROM jobs WHERE id=? AND availability='Live'",(job_id,)).fetchone()
        if not row:continue
        provider=provider_for_url(row['canonical_url'],config['direct_requisition_rules'])
        if not provider or provider=='employer_site':continue
        check=verify_direct_requisition(row['canonical_url'],provider)
        result={'job_id':job_id,'direct_url':row['canonical_url'],'provider':provider,**check}
        if check.get('verified'):
            normalized={'source':row['source'],'employer':check.get('employer') or row['employer'],'title':check['title'],
                'location':check.get('location') or row['location'],'requisition_id':row['requisition_id'] or check.get('requisition_id'),
                'canonical_url':row['canonical_url'],'discovered_at':row['discovered_at'] or check['checked_at'],
                'posted_at':check.get('posted_at'),'checked_at':check['checked_at'],'jd_text':check['jd_text'],
                'availability':'Live','provenance':{'provider':provider,'direct_url':row['canonical_url'],'verification_evidence':check['evidence']}}
            try:
                updated_id,_=job_db.create_job(db,normalized);result['job_id']=updated_id;result['persisted']=True
            except ValueError as exc:result.update({'persisted':False,'failure_reason':str(exc)})
        else:result['persisted']=False
        results.append(result)
    return {'checked':len(results),'verified_and_refreshed':sum(bool(x.get('verified') and x.get('persisted')) for x in results),'results':results}

def resolve(db,config,limit=10,target_ids=None,target_urls=None,recheck_ats=True):
    stamp=dt.datetime.now(dt.timezone.utc).isoformat()
    missing_ats=recheck_missing_ats_jds(db,config,limit=7) if recheck_ats else {'checked':0,'results':[]}
    rows=db.execute('SELECT canonical_url,source,title FROM lead_queue').fetchall()
    invalid=0
    for row in rows:
        state=classify(row['source'],row['canonical_url'])
        if state=='NotAJob':
            db.execute("UPDATE lead_queue SET state='NotAJob' WHERE canonical_url=?",(row['canonical_url'],))
            invalid+=1
    for prior in db.execute("SELECT canonical_url,status,result_json FROM lead_checks").fetchall():
        observation=json.loads(prior['result_json'] or '{}')
        status=prior['status']
        if status=='Unavailable' and 'not found' not in str(observation.get('page_title') or '').lower():
            # Older resolver versions conflated an absent DOM excerpt with a
            # closed posting. Re-label conservatively; the job may still be live.
            status='PortalDetailsUnavailable'
            db.execute("UPDATE lead_checks SET status='PortalDetailsUnavailable' WHERE canonical_url=?",(prior['canonical_url'],))
        if 'not found' in str(observation.get('page_title') or '').lower() and status=='NoAllowlistedOriginal':
            status='Unavailable'
            db.execute("UPDATE lead_checks SET status='Unavailable' WHERE canonical_url=?",(prior['canonical_url'],))
        if status in ('NoAllowlistedOriginal','OriginalMatched','Unavailable','Blocked','PortalDetailsUnavailable','PortalRedirect'):
            db.execute('UPDATE lead_queue SET state=? WHERE canonical_url=?',(status,prior['canonical_url']))
    enabled_sources=[name for name,record in config['sources'].items() if record['enabled']]
    source_marks=','.join('?' for _ in enabled_sources)
    pending_all=db.execute(f"SELECT * FROM lead_queue WHERE state IN ('NeedsOriginal','NoAllowlistedOriginal','PortalDetailsUnavailable') AND source IN ({source_marks}) AND canonical_url NOT IN (SELECT canonical_url FROM lead_checks WHERE status IN ('OriginalMatched','Blocked'))",enabled_sources).fetchall()
    if target_urls is not None:
        wanted_urls=set(target_urls)
        pending_all=[r for r in pending_all if r['canonical_url'] in wanted_urls]
    if target_ids:
        wanted={str(value) for value in target_ids}
        pending_all=[r for r in pending_all if urlsplit(r['canonical_url']).path.rstrip('/').split('/')[-1] in wanted]
    pending=select_pending(pending_all,limit)
    for row in pending:
        db.execute("UPDATE lead_queue SET state='NeedsOriginal' WHERE canonical_url=?",(row['canonical_url'],))
    results=[]
    with futures.ThreadPoolExecutor(max_workers=min(3,len(pending)) or 1) as pool:
        work={pool.submit(inspect_portal,row['source'],row['canonical_url']):row for row in pending}
        for future in futures.as_completed(work):
            row=work[future];observation=future.result()
            creation_failure=None
            candidates=[]
            for item in observation.get('external_origins',[]):
                url=item.get('url') if isinstance(item,dict) else item
                link_text=item.get('text','') if isinstance(item,dict) else ''
                provider=provider_for_url(url,config['direct_requisition_rules'],link_text,urlsplit(row['canonical_url']).hostname)
                if provider:candidates.append((url,provider,item.get('text','') if isinstance(item,dict) else ''))
            for item in observation.get('apply_links',[]):
                url=item.get('url');link_text=item.get('text','')
                provider=provider_for_url(url,config['direct_requisition_rules'],link_text,urlsplit(row['canonical_url']).hostname)
                if provider:candidates.append((url,provider,link_text))
            # Resolve the exact linked ATS requisition. A 200 page with only a
            # title is recorded as live-but-unreviewed and never enters fit review.
            checks=[];matched=None;resolved_job=None
            max_external=config['direct_requisition_rules']['employer_site']['max_links_per_lead']
            for url,provider,link_text in candidates[:max_external]:
                direct=(verify_linked_employer_requisition(url,urlsplit(row['canonical_url']).hostname,
                        config['direct_requisition_rules']['employer_site']) if provider=='employer_site'
                        else verify_direct_requisition(url,provider))
                direct.update({'provider':provider,'portal_link_text':link_text})
                checks.append(direct)
                if direct.get('verified'):
                    # A direct apply link is bound to this portal card; require
                    # meaningful title overlap to avoid cross-job linking.
                    a=set(re.findall(r'[a-z0-9]+',(row['title'] or '').lower()))
                    b=set(re.findall(r'[a-z0-9]+',(direct.get('title') or '').lower()))
                    overlap=len(a&b)/max(1,min(len(a),len(b)))
                    if overlap>=0.4 or not a or not b:
                        matched=url;resolved_job=(direct,provider,overlap);break
                    direct['failure_reason']='portal_and_original_titles_do_not_match'
            inspected=observation['status'] in ('Inspected','InspectedTitleOnly')
            status='OriginalMatched' if matched else 'DirectJDUnavailable' if inspected and candidates else 'NoAllowlistedOriginal' if inspected else observation['status']
            if resolved_job:
                direct,provider,overlap=resolved_job
                path=urlsplit(matched).path.strip('/').split('/')
                employer=direct.get('employer') or (observation.get('employer_label') or '').strip() or path[0].replace('-',' ').title()
                normalized={'source':'portal:'+row['source'],'employer':employer,'title':direct['title'],
                    'location':direct.get('location') or observation.get('location') or 'Not stated','requisition_id':direct.get('requisition_id'),
                    'canonical_url':matched,'discovered_at':direct['checked_at'],
                    'posted_at':direct.get('posted_at'),'checked_at':direct['checked_at'],'jd_text':direct['jd_text'],
                    'availability':'Live','provenance':{'portal_source':row['source'],'portal_url':row['canonical_url'],
                    'provider':provider,'direct_url':matched,'verification_evidence':direct['evidence'],
                    'portal_posted_text':observation.get('posted_text'),'title_overlap':round(overlap,3)}}
                try:
                    job_id,created=job_db.create_job(db,normalized)
                    if created:job_db.transition(db,job_id,'Verified','portal_original_verifier','Portal-linked exact ATS requisition live with substantive JD',matched,'Discovered')
                    direct['job_id']=job_id
                except ValueError as exc:
                    matched=None;status='HistoricalOrDuplicate';creation_failure=str(exc)
            if matched:db.execute("UPDATE lead_queue SET state='OriginalMatched' WHERE canonical_url=?",(row['canonical_url'],))
            elif status=='HistoricalOrDuplicate':db.execute("UPDATE lead_queue SET state='HistoricalOrDuplicate' WHERE canonical_url=?",(row['canonical_url'],))
            elif status in ('NoAllowlistedOriginal','DirectJDUnavailable','Blocked','Unavailable','PortalDetailsUnavailable','PortalRedirect'):
                db.execute('UPDATE lead_queue SET state=? WHERE canonical_url=?',(status,row['canonical_url']))
            checked=dt.datetime.now(dt.timezone.utc).isoformat()
            failure_reason=(None if matched else creation_failure or
                next((c.get('failure_reason') or c.get('error_kind') or c.get('status') for c in reversed(checks)),None) or
                ('no direct requisition link on inspected portal page' if inspected else observation.get('error') or observation['status']))
            payload={'source':row['source'],'title':row['title'],'original_url':matched,'direct_checks':checks,
                     'failure_reason':failure_reason,**observation}
            db.execute('INSERT OR REPLACE INTO lead_checks(canonical_url,status,checked_at,result_json) VALUES(?,?,?,?)',
                       (row['canonical_url'],status,checked,json.dumps(payload)))
            results.append({'source':row['source'],'portal_url':row['canonical_url'],'portal_title':row['title'],'status':status,'original_url':matched,
                            'employer_label':observation.get('employer_label'),'direct_checks':checks,
                            'failure_reason':payload['failure_reason']})
    remaining=db.execute("SELECT * FROM lead_queue WHERE state='NeedsOriginal'").fetchall()
    relevant=re.compile(r'java|spring|backend|back.end|microservices?|\bapi\b|software engineer|software developer|\bsde\b',re.I)
    title_relevant=sum(bool(relevant.search(row['title'] or '')) for row in remaining)
    failed_relevant=sum(x['status']!='OriginalMatched' and bool(relevant.search(x.get('portal_title') or '')) for x in results)
    portal_employers={}
    for item in results:
        label=item.get('employer_label') or 'portal company unavailable'
        portal_employers[label]=portal_employers.get(label,0)+1
    provider_counts={};employer_counts={}
    for item in results:
        for check in item.get('direct_checks',[]):
            if check.get('verified'):
                provider_counts[check['provider']]=provider_counts.get(check['provider'],0)+1
                employer=check.get('employer') or 'employer not stated'
                employer_counts[employer]=employer_counts.get(employer,0)+1
    return {'navigation_links_reclassified':invalid,'missing_ats_jd_refresh':missing_ats,
            'direct_provider_coverage':provider_counts,'direct_employer_coverage':employer_counts,
            'portal_employer_coverage':portal_employers,'target_title_failures_in_batch':failed_relevant,
            'portal_jobs_inspected':len(results),
            'originals_matched':sum(x['status']=='OriginalMatched' for x in results),
            'substantive_originals_verified':sum(x['status']=='OriginalMatched' for x in results),
            'live_title_only':sum(any(c.get('status')=='LiveTitleOnly' for c in x.get('direct_checks',[])) for x in results),
            'direct_check_failures':sum(bool(x.get('failure_reason')) for x in results),
            'needs_original':db.execute("SELECT count(*) FROM lead_queue WHERE state='NeedsOriginal'").fetchone()[0],
            'title_relevant_unresolved':title_relevant,
            'by_source':{name:{'needs_original':db.execute("SELECT count(*) FROM lead_queue WHERE state='NeedsOriginal' AND source=?",(name,)).fetchone()[0],
                               'not_a_job':db.execute("SELECT count(*) FROM lead_queue WHERE state='NotAJob' AND source=?",(name,)).fetchone()[0]}
                         for name in config['sources']},'results':results}

def _portal_job_id(url):
    match=re.fullmatch(r'/jobs/view/(\d+)/?',urlsplit(url).path)
    return match.group(1) if match else None

def _role_similarity(a,b):
    left=set(re.findall(r'[a-z0-9]+',str(a or '').lower()))
    right=set(re.findall(r'[a-z0-9]+',str(b or '').lower()))
    return len(left&right)/max(1,min(len(left),len(right)))

def visible_portal_location(title,observation):
    """Return location only when it belongs to this exact visible job."""
    header=(observation.get('job_header_excerpt') or '').strip()
    lines=[line.strip() for line in header.splitlines() if line.strip()]
    if len(lines)>1 and lines[0].casefold()==title.strip().casefold():
        value=lines[1].split('·',1)[0].strip()
        if len(value)<120 and re.search(r'\b(?:India|United States|Pune|Bengaluru|Bangalore|Hyderabad|Delhi|Noida|Gurugram|Mumbai|Chennai)\b',value,re.I):
            return value,'job_header_exact_title'
    raw_header_location=(observation.get('location') or '').strip()
    if raw_header_location:
        parts=[part.strip() for part in re.split(r'\s*[·|]\s*',raw_header_location) if part.strip()]
        location=next((part for part in parts if re.search(r'\b(?:India|United States|Pune|Bengaluru|Bangalore|Hyderabad|Delhi|Noida|Gurugram|Mumbai|Chennai)\b',part,re.I)),None)
        if location:return location,'job_header'
    # The old capture starts at About the job and includes LinkedIn's own
    # same-job alert line. Stop before recommended jobs to avoid cross-job data.
    detail=(observation.get('jd_excerpt') or '').split('More jobs',1)[0]
    labelled=re.search(r'\bLocation\s*:\s*([^\n|·]{2,130})',detail,re.I)
    labelled_value=None
    if labelled:
        labelled_value=re.split(r'\b(?:Work Type|Employment Type|Job Type)\s*:',labelled.group(1),maxsplit=1,flags=re.I)[0].strip()
        if labelled_value and re.search(r'\b(?:India|United States|Pune|Bengaluru|Bangalore|Hyderabad|Delhi|Noida|Gurugram|Mumbai|Chennai)\b',labelled_value,re.I):
            return labelled_value,'job_description_location_label'
    alert=re.search(r'Set alert for similar jobs\s*\n+([^\n]{3,160})\s*\n+Off\b',detail,re.I)
    line=(observation.get('same_job_alert') or (alert.group(1) if alert else '')).strip()
    if line:
        prefix=title.strip()+','
        if line.casefold().startswith(prefix.casefold()):
            value=line[len(prefix):].strip()
            if value:return value,'same_job_alert_exact_title'
    if labelled_value:return labelled_value,'job_description_location_label_no_country'
    return None,None

def portal_job_description(observation):
    """Discard LinkedIn recommendation and company widgets after the actual JD."""
    raw=observation.get('jd_excerpt') or ''
    markers=[m.start() for m in re.finditer(r'\n(?:About the company|Set alert for similar jobs|More jobs)\s*\n',raw,re.I)
             if m.start()>250]
    return raw[:min(markers)] if markers else raw

APPLICATION_OUTCOMES=('Applied','Interview','Offer','Rejected','Withdrawn')
UNRESOLVED_ATTEMPTS=('uncertain','incomplete')
NEAR_IDENTICAL=0.9

def _job_ids(row):
    """Exact identifiers of an existing job, by namespace (employer requisition vs. portal job id)."""
    ids={}
    if row['requisition_id']:ids['req']=str(row['requisition_id'])
    try:provenance=json.loads(row['provenance_json'] or '{}')
    except ValueError:provenance={}
    if provenance.get('portal') and provenance.get('portal_job_id'):
        ids['portal:'+str(provenance['portal'])]=str(provenance['portal_job_id'])
    return ids

def _portal_duplicate_check(db,employer,title,portal_id,employer_req_id,portal=None,exclude_job_id=None):
    """Exact identifiers decide; title similarity only covers records that cannot be compared by ID.

    * exact portal / employer requisition match -> duplicate (unchanged).
    * an existing record that is not application history (a mere discovered/queued lead) never counts.
    * two records with different exact IDs in the same namespace are different postings, unless an earlier
      attempt is still unresolved (uncertain/incomplete) and the titles are near-identical.
    """
    import job_db
    company_key=job_db.employer(employer)
    if portal_id:
        for row in db.execute('SELECT id,provenance_json FROM jobs').fetchall():
            if row['id']==exclude_job_id:continue
            try:provenance=json.loads(row['provenance_json'] or '{}')
            except ValueError:continue
            if str(provenance.get('portal_job_id') or '')==str(portal_id):
                return {'status':'exact_portal_id','match_job_id':row['id']}
    if employer_req_id:
        req_key=job_db.identity(employer,employer_req_id)[1]
        match=db.execute('SELECT id FROM jobs WHERE employer_key=? AND requisition_key=? AND id!=?',(company_key,req_key,exclude_job_id or -1)).fetchone()
        history=db.execute('SELECT 1 FROM history_keys WHERE employer_key=? AND requisition_key=?',(company_key,req_key)).fetchone()
        if match or history:return {'status':'exact_employer_requisition_id','match_job_id':match['id'] if match else None}
    mine={}
    if portal_id and portal:mine['portal:'+portal]=str(portal_id)
    if employer_req_id:mine['req']=str(employer_req_id)
    uncertain=[];compared_by_id=0
    for row in db.execute("""SELECT j.id,j.title,j.employer,j.state,j.requisition_id,j.provenance_json,
            (SELECT a.state FROM submission_attempts a WHERE a.job_id=j.id) AS attempt_state
            FROM jobs j WHERE j.employer_key=?""",(company_key,)).fetchall():
        applied=row['state'] in APPLICATION_OUTCOMES or row['attempt_state'] is not None
        if row['id']==exclude_job_id:continue
        if not applied:continue
        similarity=_role_similarity(title,row['title'])
        theirs=_job_ids(row)
        shared=[ns for ns in mine if ns in theirs]
        if shared and all(mine[ns]!=theirs[ns] for ns in shared):
            compared_by_id+=1
            if not (row['attempt_state'] in UNRESOLVED_ATTEMPTS and similarity>=NEAR_IDENTICAL):continue
        if similarity>=0.72:uncertain.append({'job_id':row['id'],'role':row['title'],'similarity':round(similarity,3)})
    for row in db.execute('SELECT * FROM history_keys WHERE employer_key=?',(company_key,)).fetchall():
        if row['terminal_status'] not in APPLICATION_OUTCOMES:continue   # Closed/Excluded rows are not applications
        historical_ids={}
        parsed=urlsplit(row['direct_link'] or '')
        historical_portal='naukri' if parsed.hostname in ('www.naukri.com','naukri.com') else 'linkedin' if parsed.hostname in ('www.linkedin.com','linkedin.com') else None
        historical_id=(re.search(r'-(\d{8,})/?$',parsed.path) if historical_portal=='naukri'
                       else re.search(r'/jobs/view/(?:[^/]*-)?(\d{8,})/?$',parsed.path) if historical_portal=='linkedin' else None)
        if historical_portal and historical_id:historical_ids['portal:'+historical_portal]=historical_id.group(1)
        if row['requisition_id'] and employer_req_id:historical_ids['req']=str(row['requisition_id'])
        shared=[ns for ns in mine if ns in historical_ids]
        if shared and any(mine[ns]==historical_ids[ns] for ns in shared):
            return {'status':'exact_portal_id' if 'portal:'+str(portal) in shared else 'exact_employer_requisition_id',
                    'match_requisition_id':row['requisition_id'],'match_link':row['direct_link'],'match_source':row['source']}
        if shared and all(mine[ns]!=historical_ids[ns] for ns in shared):
            # Unresolved attempts are already conservatively checked above.
            compared_by_id+=1;continue
        if row['requisition_id'] and employer_req_id and str(row['requisition_id'])!=str(employer_req_id):
            compared_by_id+=1;continue
        similarity=_role_similarity(title,row['role'])
        if similarity>=0.72:uncertain.append({'role':row['role'],'status':row['terminal_status'],'source':row['source'],
            'requisition_id':row['requisition_id'],'direct_link':row['direct_link'],'match_basis':'title_similarity_without_distinct_comparable_ids','similarity':round(similarity,3)})
    return {'status':'uncertain' if uncertain else 'clear','possible_matches':uncertain,'compared_by_exact_id':compared_by_id}

def review_portal_native(db,config,target_ids,limit=29,saved_observations=None):
    """Review LinkedIn job pages as a distinct source, without opening Apply/forms."""
    import review_candidates
    wanted={str(x) for x in target_ids};rows=db.execute("SELECT * FROM lead_queue WHERE source='linkedin'").fetchall()
    selected=[r for r in rows if _portal_job_id(r['canonical_url']) in wanted][:max(0,min(limit,29))]
    results=[];accepted_ids=[]
    # Keep browser concurrency bounded and independent of form workers. All
    # database writes happen below on this thread after observations complete.
    if saved_observations is None:
        with futures.ThreadPoolExecutor(max_workers=min(3,len(selected)) or 1) as pool:
            observations=list(pool.map(lambda row: inspect_portal('linkedin',row['canonical_url']),selected))
    else:
        observations=[saved_observations[row['canonical_url']] for row in selected]
    for row,observation in zip(selected,observations):
        portal_id=_portal_job_id(row['canonical_url']);reasons=[]
        if observation.get('status') in ('Blocked','PortalRedirect','Unavailable','PortalError'):
            reasons.append(observation.get('error') or observation['status'])
        title=(observation.get('job_title') or '').strip();employer=(observation.get('employer_label') or '').strip()
        location,location_evidence=visible_portal_location(title,observation)
        jd=portal_job_description(observation)
        if not portal_id:reasons.append('LinkedIn job ID missing from exact /jobs/view/{id} URL')
        if not employer:reasons.append('Employer name not visible on the job page')
        if not title:reasons.append('Job title not visible on the job page')
        if not location:reasons.append('India location not visible or established on the job page')
        elif not review_candidates.INDIA.search(location):
            reasons.append((f'Explicit job location is outside India: {location[:100]}'
                if re.search(r'\b(?:United States|USA|US|Canada|Europe|UK)\b',location,re.I)
                else f'India eligibility not established by job location: {location[:100]}'))
        if location_evidence=='same_job_alert_exact_title':
            reasons.append('Similar-jobs alert location is not independent job-header or exact-card evidence')
        if not review_candidates.substantive_jd(jd):reasons.append('Visible job page lacks substantive duties and qualifications text')
        controls=observation.get('application_controls',[])
        easy=[x for x in controls if x.get('text','').strip().lower()=='easy apply' and not x.get('disabled')]
        portal_apply=[x for x in controls if x.get('text','').strip().lower()=='apply' and not x.get('disabled')]
        disabled_easy=[x for x in controls if x.get('text','').strip().lower()=='easy apply' and x.get('disabled')]
        active=bool(easy or portal_apply) and not observation.get('listing_unavailable')
        method=('LinkedIn Easy Apply' if easy else 'LinkedIn Apply (external destination; not opened)'
                if portal_apply else 'LinkedIn Easy Apply (disabled)' if disabled_easy else None)
        if observation.get('listing_unavailable'):reasons.append('Portal listing says it is no longer accepting applications')
        elif not active:reasons.append('No enabled LinkedIn Easy Apply or Apply action visible')
        duplicate=None
        employer_req_id=observation.get('employer_requisition_id')
        verified=not reasons
        status='PortalNativeVerified' if verified else 'PortalNativeUnverified'
        job_id=None
        if verified:
            duplicate=_portal_duplicate_check(db,employer,title,portal_id,employer_req_id,portal='linkedin')
            if duplicate['status'] in ('exact_portal_id','exact_employer_requisition_id'):
                status='PortalNativeDuplicate'
                reasons.append('Exact portal or employer requisition already exists in application history')
            else:
                checked=(observation.get('checked_at') if saved_observations is not None else None) or dt.datetime.now(dt.timezone.utc).isoformat()
                provenance={'verification_method':'portal_native','portal':'linkedin','portal_job_id':portal_id,
                    'portal_url':row['canonical_url'],'page_checked_at':checked,'live_apply_status':'Live; enabled portal Apply action visible',
                    'application_method':method,'employer_requisition_id':employer_req_id,
                    'location_evidence':location_evidence,'verification_observation':'saved_capture' if saved_observations is not None else 'live_page',
                    'portal_posted_text':observation.get('posted_text'),'portal_page_title':observation.get('page_title'),
                    'duplicate_check':duplicate,'jd_evidence':{'visible_characters':len(jd),
                    'duties_and_qualification_sections_detected':re.findall(r'responsibilit\w*|qualifications?|requirements|what you.?ll do|duties',jd,re.I)}}
                record={'source':'linkedin_portal_native','employer':employer,'title':title,'location':location,
                    'requisition_id':employer_req_id,'canonical_url':row['canonical_url'],'discovered_at':row['first_seen'],
                    'posted_at':None,'checked_at':checked,'jd_text':jd,'availability':'Live','provenance':provenance}
                try:
                    job_id,created=job_db.create_job(db,record)
                    if created:job_db.transition(db,job_id,'Verified','linkedin_portal_verifier',
                        'Portal-native listing has visible identity, substantive JD, live status, and enabled Apply action; employer page not checked',
                        row['canonical_url'],'Discovered')
                    accepted_ids.append(job_id);provenance['database_job_id']=job_id
                except ValueError as exc:
                    status='PortalNativeDuplicate';reasons.append(str(exc));verified=False
        payload={**observation,'portal_url':row['canonical_url'],'portal_job_id':portal_id,'source':'linkedin','status':status,
            'employer':employer or None,'title':title or None,'location':location or None,'location_evidence':location_evidence,'jd_text':jd or None,
            'live_apply_status':'Live; enabled portal Apply action visible' if active else 'Not verified active',
            'application_method':method,'employer_requisition_id':employer_req_id,'duplicate_check':duplicate,
            'reasons':reasons,'provenance_type':'portal_native','employer_page_checked':False,
            'checked_at':(observation.get('checked_at') if saved_observations is not None else None) or dt.datetime.now(dt.timezone.utc).isoformat(),
            'database_job_id':job_id}
        db.execute('INSERT OR REPLACE INTO lead_checks(canonical_url,status,checked_at,result_json) VALUES(?,?,?,?)',
            (row['canonical_url'],status,payload['checked_at'],json.dumps(payload,ensure_ascii=False)))
        db.execute('UPDATE lead_queue SET state=? WHERE canonical_url=?',(status,row['canonical_url']))
        results.append(payload)
    queued=review_candidates.enqueue(db,accepted_ids)
    reviewed=review_candidates.consume(db,config,limit=min(limit,config['review']['max_jds_per_run']),
                                       per_company=config['review']['per_company_cap'],allowed_job_ids=accepted_ids)
    return {'verification_path':'portal_native','employer_pages_checked':0,'selected':len(selected),
        'portal_native_verified':sum(x['status']=='PortalNativeVerified' for x in results),
        'active_application_action':sum(x['live_apply_status'].startswith('Live') for x in results),
        'active_easy_apply':sum(x.get('application_method')=='LinkedIn Easy Apply' for x in results),
        'fit_review_eligible':len(accepted_ids),'exact_duplicate_blocked':sum(x['status']=='PortalNativeDuplicate' for x in results),
        'unverified':sum(x['status']=='PortalNativeUnverified' for x in results),'review_queue_added':queued,
        'fit_reviewed':len(reviewed),'fit_counts':{name:sum(x['fit']==name for x in reviewed) for name in ('High','Medium','Low','Unresolved')},
        'queued_for_resume':sum(x['fit']=='High' and x.get('eligible_for_resume') for x in reviewed),
        'review_results':reviewed,'results':results}
