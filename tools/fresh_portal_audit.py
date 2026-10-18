"""One bounded, read-only search across the five configured job portals."""
import concurrent.futures as futures
import datetime as dt
import fcntl
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs,urlsplit

import browser_portals
import job_config
import job_db
import lead_resolution
import review_candidates
from local_paths import ROOT

STATE=ROOT/'.workflow'
TERMS=['Java Spring Boot backend engineer','Java backend developer API']
LOCATIONS=['India','Bengaluru']
RECENT_SUFFIX={'linkedin':'&f_TPR=r604800','indeed':'&fromage=7'}

def _fetch(name,record,search,deadline):
    scoped=dict(record,max_queries=2,max_pages=1)
    scoped['search_url']+=RECENT_SUFFIX.get(name,'')
    return browser_portals.fetch_portal(name,scoped,search,run_number=0,deadline=deadline)

def _portal_id(source,url):
    parsed=urlsplit(url)
    if source=='linkedin':return lead_resolution._portal_job_id(url)
    if source=='indeed':return parse_qs(parsed.query).get('jk',[None])[0]
    if source=='naukri':
        found=re.search(r'-(\d{8,})/?$',parsed.path)
        return found.group(1) if found else None
    if source=='foundit':
        found=re.search(r'(?:-job-|/job/).*?(\d{5,})/?$',parsed.path)
        return found.group(1) if found else None
    return None

def _detail(source,lead):
    observation=lead_resolution.inspect_portal(source,lead['url'])
    title=(observation.get('job_title') or '').strip()
    employer=(observation.get('employer_label') or '').strip()
    location,location_evidence=lead_resolution.visible_portal_location(title,observation)
    if (not location or location_evidence=='same_job_alert_exact_title') and lead.get('card_location'):
        location,location_evidence=lead['card_location'],'search_card_exact_url'
    jd=lead_resolution.portal_job_description(observation)
    controls=observation.get('application_controls') or []
    active=any(not c.get('disabled') and re.fullmatch(r'apply(?: now)?|easy apply',c.get('text','').strip(),re.I)
               for c in controls) and not observation.get('listing_unavailable')
    reasons=[]
    if observation.get('status') not in ('Inspected','InspectedTitleOnly'):
        reasons.append(observation.get('error') or observation.get('status') or 'Page was not inspected')
    if not title:reasons.append('Job title not visible')
    if not employer:reasons.append('Employer not visible')
    elif re.fullmatch(r'(?:LinkedIn|Naukri(?:\.com)?|Foundit|Indeed|Cutshort)(?:\s+Jobs)?',employer,re.I):
        reasons.append('Employer field shows portal branding, not the hiring company')
    if title and lead_resolution._role_similarity(title,lead.get('title'))<0.5:
        reasons.append('Detail title does not match the exact search-result card')
    if not location:reasons.append('Job-specific location not visible')
    elif not review_candidates.INDIA.search(location):reasons.append('Job-specific location is not established in India')
    if location_evidence=='same_job_alert_exact_title':reasons.append('Similar-jobs alert is not job-header or exact-card location evidence')
    if not review_candidates.substantive_jd(jd):reasons.append('No substantive visible duties and qualifications')
    if not active:reasons.append('No enabled Apply action on the job page')
    portal_id=_portal_id(source,lead['url'])
    if not portal_id:reasons.append('Source-specific portal job ID not visible in exact URL')
    verified=not reasons
    fit=None
    if verified:
        row={'title':title,'jd_text':jd,'location':location,'checked_at':job_db.now()}
        fit=review_candidates.assess(row,job_config.load())
    return {'source':source,'url':lead['url'],'search_title':lead['title'],'portal_job_id':portal_id,
            'title':title or None,'employer':employer or None,'location':location,'location_evidence':location_evidence,
            'card_location':lead.get('card_location'),'inspection_status':observation.get('status'),
            'substantive_jd':review_candidates.substantive_jd(jd),'jd_characters':len(jd),
            'active_apply':active,'application_method':next((c.get('text') for c in controls if not c.get('disabled')),None),
            'verified':verified,'fit':fit,'reasons':reasons,'jd_text':jd if verified else None}

def run():
    cfg=job_config.load()
    if cfg['mode']!='read_only' or cfg['submissions']['enabled']:
        raise ValueError('Portal audit requires discovery-only mode')
    control=json.loads((STATE/'control.json').read_text())
    if control.get('paused'):raise ValueError('Workflow is paused')
    lock=STATE/'daily-run.lock';lock.parent.mkdir(parents=True,exist_ok=True)
    with lock.open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        started=job_db.now();deadline=time.monotonic()+420
        search=dict(cfg['search'],terms=TERMS,locations=LOCATIONS)
        enabled={name:record for name,record in cfg['sources'].items() if record['enabled']}
        results={}
        with futures.ThreadPoolExecutor(max_workers=min(3,len(enabled))) as pool:
            work={pool.submit(_fetch,name,record,search,deadline):name for name,record in enabled.items()}
            for future in futures.as_completed(work):
                name=work[future]
                try:results[name]=future.result()
                except Exception as exc:results[name]={'source':name,'status':'Failed','pages':[],'leads':[],
                    'error_kind':type(exc).__name__}
        selected=[];summaries={};db=job_db.connect();job_db.initialize(db)
        try:
            for name,result in results.items():
                exact={lead['url']:lead for lead in result.get('leads',[])
                       if lead_resolution.classify(name,lead.get('url'))=='NeedsOriginal'}
                ranked=sorted(exact.values(),key=lambda lead:lead_resolution.lead_priority(
                    {'title':lead['title'],'last_seen':started,'first_seen':started}),reverse=True)
                targeted=[lead for lead in ranked if re.search(r'java|spring|backend|back.end|software engineer|software developer|api',lead['title'],re.I)]
                chosen=targeted[:2]
                selected.extend((name,lead) for lead in chosen)
                for lead in exact.values():
                    db.execute("INSERT INTO lead_queue(canonical_url,source,title,first_seen,last_seen,state) VALUES(?,?,?,?,?,?) ON CONFLICT(canonical_url) DO UPDATE SET last_seen=excluded.last_seen",
                               (lead['url'],name,lead['title'],started,started,lead_resolution.classify(name,lead['url'])))
                bounded=cfg['sources'][name]['max_queries']>2 or cfg['sources'][name]['max_pages']>1
                coverage_status='Partial' if result['status']=='Success' and bounded else result['status']
                summaries[name]={'status':coverage_status,'fetch_status':result['status'],
                    'coverage_reason':'Two queries and one page each, below configured coverage' if bounded else None,
                    'error_kind':result.get('error_kind'),
                    'search_pages':result.get('pages',[]),'raw_links':len(result.get('leads',[])),
                    'unique_job_leads':len(exact),'target_title_leads':len(targeted),
                    'search_card_locations':sum(bool(x.get('card_location')) for x in exact.values()),
                    'detail_pages_inspected':0,'substantive_jds':0,'detail_locations':0,
                    'verified_roles':0,'high_fit':0,'low_fit':0,'unresolved_fit':0,
                    'recent_filter_requested':('past 7 days' if name in RECENT_SUFFIX else 'No verified URL filter; configured search order'),
                    'details':[]}
        finally:db.close()
        with futures.ThreadPoolExecutor(max_workers=3) as pool:
            work={pool.submit(_detail,name,lead):name for name,lead in selected}
            for future in futures.as_completed(work):
                name=work[future]
                try:detail=future.result()
                except Exception as exc:detail={'source':name,'inspection_status':'Failed','verified':False,
                                                'substantive_jd':False,'location':None,'reasons':[type(exc).__name__]}
                source=summaries[name];source['details'].append(detail);source['detail_pages_inspected']+=1
                source['substantive_jds']+=bool(detail.get('substantive_jd'))
                source['detail_locations']+=bool(detail.get('location'))
                source['verified_roles']+=bool(detail.get('verified'))
                if detail.get('fit'):
                    source['high_fit']+=detail['fit']['fit']=='High'
                    source['low_fit']+=detail['fit']['fit']=='Low'
                    source['unresolved_fit']+=detail['fit']['fit']=='Unresolved'
        report={'started_at':started,'finished_at':job_db.now(),'mode':'read_only','search_terms':TERMS,
                'locations':LOCATIONS,'detail_cap_per_portal':2,'pages_per_portal':2,
                'browser_forms_opened':0,'uploads':0,'submissions':0,'sources':summaries}
        path=STATE/'reviews'/(dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')+'-fresh-portal-audit.json')
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(report,indent=2,ensure_ascii=False,default=str)+'\n')
        return path,report

if __name__=='__main__':
    path,report=run()
    print(json.dumps({'report':str(path.relative_to(ROOT)),
        'by_portal':{name:{key:value for key,value in source.items() if key in
            ('status','error_kind','raw_links','unique_job_leads','target_title_leads','search_card_locations',
             'detail_pages_inspected','substantive_jds','detail_locations','verified_roles','high_fit','low_fit','unresolved_fit','recent_filter_requested')}
            for name,source in report['sources'].items()}},indent=2))
