"""Read-only verification of exact Naukri and Cutshort job pages."""
import concurrent.futures as futures
import datetime as dt
import json
import re
import sqlite3
import time
from urllib.parse import urlsplit

import job_db
import lead_resolution
import review_candidates
from browser_portals import parse_json_result
from parallel_browser_probe import MCP
from tab_lifecycle import OwnedTabs

DETAIL_DOM=r'''() => {
  const body=document.body?.innerText||'';
  const host=location.hostname;
  const title=(document.querySelector('h1')?.innerText||'').trim();
  let employer='',jobLocation='',experience='',jd='';
  if(host==='www.naukri.com'){
    const blocks=[...document.querySelectorAll('[class*="job-desc"]')]
      .map(e=>(e.innerText||'').trim()).filter(x=>x.length>250);
    jd=blocks.sort((a,b)=>b.length-a.length)[0]||'';
    const before=body.slice(0,body.indexOf('Job highlights')>0?body.indexOf('Job highlights'):900);
    jobLocation=(before.match(/\b(?:Pune|Bengaluru|Bangalore|Hyderabad|Delhi|Noida|Gurugram|Mumbai|Chennai)(?:\s*,\s*(?:Pune|Bengaluru|Bangalore|Hyderabad|Delhi|Noida|Gurugram|Mumbai|Chennai))*/i)||[])[0]||'';
    experience=(before.match(/\b\d+(?:\.\d+)?\s*(?:[-–]\s*\d+(?:\.\d+)?)?\s*(?:years?|yrs?)\b/i)||[])[0]||'';
  }else if(host==='cutshort.io'){
    employer=(document.querySelector('h2')?.innerText||'').replace(/^at\s+/i,'').trim();
    const header=body.split('\n').find(x=>x.includes(' · ')&&x.startsWith(title+' at '))||'';
    jobLocation=(header.split(' · ')[1]||'').trim();
    experience=(header.split(' · ')[2]||'').trim();
    const start=body.indexOf('\nSkills\n'),end=start>=0?body.indexOf('\nBrowse related jobs',start):-1;
    if(start>=0&&end>start)jd=body.slice(start,end).trim();
  }
  const apply=[...document.querySelectorAll('button,a[href]')].filter(x=>
    /^(apply|apply now|apply to this job)$/i.test((x.innerText||'').trim())&&
    !x.disabled&&x.getAttribute('aria-disabled')!=='true'&&
    !!(x.offsetWidth||x.offsetHeight||x.getClientRects().length));
  return {host,url:location.href,page_title:document.title,title,employer,location:jobLocation,experience,
    jd:jd.slice(0,18000),body_characters:body.length,apply_visible:apply.length>0,
    unavailable:/no longer accepting applications|job is closed|position (?:is )?filled|this job is no longer available/i.test(body),
    blocked:/access denied|captcha|verify you are human|unusual traffic|403 forbidden/i.test(body.slice(0,1800))};
}'''

def inspect(source,lead):
    if source=='linkedin':
        import linkedin_discovery
        try:return linkedin_discovery.inspect(lead)
        except Exception as exc:return {'status':'BrowserError','error_kind':type(exc).__name__}
    if source=='naukri':
        import naukri_discovery
        try:return naukri_discovery.inspect(lead)
        except Exception as exc:return {'status':'BrowserError','error_kind':type(exc).__name__}
    client=None;owned=None
    try:
        client=MCP();owned=OwnedTabs(client,owner=f'portal-native:{source}')
        owned.open(lead['url'])
        data=parse_json_result(client.tool('browser_evaluate',{'function':DETAIL_DOM},timeout=30))
        checks=1
        while data.get('body_characters',0)<50 and checks<3:
            time.sleep(2)
            data=parse_json_result(client.tool('browser_evaluate',{'function':DETAIL_DOM},timeout=30))
            checks+=1
        data['render_checks']=checks
        return data
    except (RuntimeError,TimeoutError,ValueError,OSError) as exc:
        return {'status':'BrowserError','error_kind':type(exc).__name__}
    finally:
        if client:
            if owned:owned.close_all()
            client.close()

def _company(lead):
    if lead.get('employer'):return lead['employer']
    match=re.search(r'\nat ([^\n]+)',lead.get('card_excerpt') or '')
    return match.group(1).strip() if match else 'Unidentified '+str(lead.get('portal_job_id') or lead.get('url'))

def _priority(lead):
    title=lead.get('title') or '';card=lead.get('card_excerpt') or ''
    score=lead_resolution.lead_priority({'title':title,'first_seen':job_db.now(),'last_seen':job_db.now()})
    years=re.search(r'\b(\d+(?:\.\d+)?)\s*(?:[-–]\s*\d+(?:\.\d+)?)?\s*(?:years?|yrs?)\b',card,re.I)
    if years:score+=25 if float(years.group(1))<=3 else -30
    post=re.search(r'(\d+)\s*(day|week|month|hour)',lead.get('posted_text') or '',re.I)
    if post:
        age=int(post.group(1))*{'hour':1/24,'day':1,'week':7,'month':30}[post.group(2).lower()]
        score+=max(-100,70-age*10)
    return score+{'Fresh':1000,'Recent':500,'Unknown':0}.get(lead.get('freshness'),0)

def selection_plan(leads,limit,per_company):
    by_source={};seen=set();skips=[]
    def skip(source,lead,reason):
        skips.append({'source':source,'portal_job_id':lead.get('portal_job_id'),'url':lead.get('url'),
                      'title':lead.get('title'),'reason':reason})
    for source,lead in sorted(leads,key=lambda x:_priority(x[1]),reverse=True):
        key=(source,lead.get('portal_job_id'))
        if key in seen:skip(source,lead,'duplicate_exact_id_in_batch');continue
        seen.add(key)
        if source in ('naukri','linkedin'):
            from naukri_discovery import card_filter_reason
            reason=card_filter_reason(lead)
            if source=='linkedin' and lead.get('keyword_search_candidate') and reason=='no_relevant_title_or_generic_engineering_card_skills':reason=None
            if reason:skip(source,lead,reason);continue
        else:
            if review_candidates.NON_TARGET_TITLE.search(lead.get('title') or ''):
                skip(source,lead,'primary_role_outside_target');continue
            if not re.search(r'java|spring|backend|back.end|\bapi\b',lead.get('title') or '',re.I):
                skip(source,lead,'no_relevant_title');continue
        by_source.setdefault(source,[]).append(lead)
    selected=[];taken={}
    while len(selected)<limit and any(by_source.values()):
        for source in sorted(('naukri','linkedin','cutshort'),key=lambda s:_priority(by_source[s][0]) if by_source.get(s) else -1,reverse=True):
            items=by_source.get(source,[])
            while items:
                lead=items.pop(0);company=job_db.employer(_company(lead))
                if taken.get(company,0)>=per_company:skip(source,lead,'per_employer_jd_cap');continue
                selected.append((source,lead));taken[company]=taken.get(company,0)+1;break
            if len(selected)>=limit:break
    for source,items in by_source.items():
        for lead in items:skip(source,lead,'per_run_jd_cap')
    return selected,skips

def select(leads,limit,per_company):
    return selection_plan(leads,limit,per_company)[0]

def verify_leads(db,leads,*,limit=25,per_company=5):
    selected,selection_skips=selection_plan(leads,limit,per_company);observations=[]
    with futures.ThreadPoolExecutor(max_workers=1 if any(s in ('naukri','linkedin') for s,l in selected) else 3) as pool:
        work={pool.submit(inspect,source,lead):(source,lead) for source,lead in selected}
        for future in futures.as_completed(work):
            source,lead=work[future]
            try:observation=future.result()
            except Exception as exc:observation={'status':'BrowserError','error_kind':type(exc).__name__}
            observations.append((source,lead,observation))
    results=[];verified_ids=[]
    for source,lead,obs in observations:
        url=lead['url'];title=(obs.get('title') or '').strip();employer=(obs.get('employer') or lead.get('employer') or '').strip()
        location=(obs.get('location') or lead.get('card_location') or '').strip()
        jd=(obs.get('jd') or '').strip();reasons=[]
        parsed=urlsplit(url);observed=urlsplit(obs.get('url') or '')
        if obs.get('status')=='BrowserError':reasons.append('browser_'+obs.get('error_kind','error'))
        else:
            from urllib.parse import parse_qs
            same_path=parsed.path.rstrip('/')==observed.path.rstrip('/')
            same_linkedin_job=(source=='linkedin' and observed.path.rstrip('/')=='/jobs/search' and
                parse_qs(observed.query).get('currentJobId')==[str(lead.get('portal_job_id'))])
            if parsed.hostname!=observed.hostname or not (same_path or same_linkedin_job):
                reasons.append('exact_job_url_redirected')
        if obs.get('body_characters',0)<50:reasons.append('job_page_blank_after_render_wait')
        if obs.get('blocked'):reasons.append('site_verification_or_access_denied')
        if not title or lead_resolution._role_similarity(title,lead.get('title'))<0.5:reasons.append('job_title_missing_or_card_mismatch')
        if not employer:reasons.append('employer_not_visible')
        if not review_candidates.INDIA.search(location):reasons.append('job_specific_india_location_not_visible')
        if not review_candidates.substantive_jd(jd):reasons.append('substantive_duties_and_qualifications_not_visible')
        if obs.get('unavailable'):reasons.append('listing_says_unavailable')
        elif not obs.get('apply_visible'):reasons.append('enabled_apply_action_not_visible')
        portal_id=str(lead.get('portal_job_id') or '')
        if source in ('naukri','linkedin') and not re.fullmatch(r'\d{8,}',portal_id):reasons.append(source+'_job_id_missing')
        if source=='cutshort' and not re.fullmatch(r'[A-Za-z0-9]{8}',portal_id):reasons.append('cutshort_job_id_missing')
        job_id=None;duplicate=None
        if not reasons:
            existing=db.execute("SELECT j.* FROM jobs j WHERE canonical_url=? AND state='Verified' AND source=? AND employer_key=? AND NOT EXISTS(SELECT 1 FROM submission_attempts a WHERE a.job_id=j.id)",
                                (url,source+'_portal_native',job_db.employer(employer))).fetchone()
            same_identity=bool(existing and str(json.loads(existing['provenance_json'] or '{}').get('portal_job_id'))==portal_id)
            refresh_id=existing['id'] if same_identity else None
            duplicate=lead_resolution._portal_duplicate_check(db,employer,title,portal_id,None,portal=source,exclude_job_id=refresh_id)
            if duplicate['status'].startswith('exact_'):reasons.append('exact_portal_or_history_duplicate')
            else:
                stamp=job_db.now();experience=(obs.get('experience') or '').strip()
                full_jd=f'Location: {location}\n'+(f'Experience: {experience}\n' if experience else '')+f'\n{jd}'
                record={'source':source+'_portal_native','employer':employer,'title':title,'location':location,
                    'slug':f'{source}-{portal_id.lower()}',
                    'requisition_id':existing['requisition_id'] if same_identity else None,'canonical_url':url,'discovered_at':stamp,'posted_at':lead.get('posted_at'),
                    'checked_at':stamp,'jd_text':full_jd,'availability':'Live',
                    'provenance':{'verification_method':'portal_native','portal':source,'portal_job_id':portal_id,
                        'portal_url':url,'search_page_url':lead.get('search_page_url'),
                        'freshness':lead.get('freshness'),'posting_evidence':lead.get('posted_text'),
                        'live_apply_status':'Live; enabled portal Apply action visible','duplicate_check':duplicate,
                        'jd_evidence':{'visible_characters':len(jd),'page_title':obs.get('page_title')}}}
                try:
                    job_id,created=job_db.create_job(db,record)
                    if created:job_db.transition(db,job_id,'Verified','portal_native_verifier',
                        'Exact portal page shows identity, substantive JD, India location, and enabled Apply action',url,'Discovered')
                    verified_ids.append(job_id)
                except (ValueError,sqlite3.IntegrityError) as exc:
                    reasons.append('history_or_identity_duplicate: '+str(exc)[:150])
        status='PortalNativeVerified' if not reasons else 'PortalNativeUnverified'
        payload={'source':source,'url':url,'portal_job_id':portal_id,'title':title or lead.get('title'),
            'employer':employer or None,'location':location or None,'status':status,'reasons':reasons,
            'jd_characters':len(jd),'experience_evidence':obs.get('experience'),
            'apply_visible':bool(obs.get('apply_visible')),'database_job_id':job_id,
            'duplicate_check':duplicate,'render_checks':obs.get('render_checks'),
            'card_sha256':lead.get('card_sha256'),'freshness':lead.get('freshness'),
            'posted_at':lead.get('posted_at'),'posted_text':lead.get('posted_text')}
        db.execute('INSERT OR REPLACE INTO lead_checks(canonical_url,status,checked_at,result_json) VALUES(?,?,?,?)',
                   (url,status,job_db.now(),json.dumps(payload)))
        db.execute('UPDATE lead_queue SET state=? WHERE canonical_url=?',(status,url))
        results.append(payload)
    return {'selected':len(selected),'verified':sum(x['status']=='PortalNativeVerified' for x in results),
            'unverified':sum(x['status']!='PortalNativeVerified' for x in results),
            'verified_job_ids':verified_ids,'results':results,'selection_skips':selection_skips}
