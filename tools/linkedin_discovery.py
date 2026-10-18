"""Bounded LinkedIn Easy Apply discovery and exact JD reads; no form actions."""
import datetime as dt
import json
import os
import re
import time
from urllib.parse import quote
from local_paths import ROOT
from linkedin_browser import chrome,status
from naukri_discovery import readonly_route,card_filter_reason
from browser_portals import _routes

CARDS=r'''()=>{
 const cards=[...document.querySelectorAll('li[data-occludable-job-id],li[data-job-id],div.job-card-container[data-job-id],.base-search-card')];
 const seen=new Set(),links=[];
 for(const c of cards){const a=[...c.querySelectorAll('a[href]')].find(a=>/\/jobs\/view\/(?:[^/?]*-)?\d+/.test(a.href));
 const id=c.getAttribute('data-occludable-job-id')||c.getAttribute('data-job-id')||(a?.href.match(/\/jobs\/view\/(?:[^/?]*-)?(\d+)/)||[])[1];
 if(!id||seen.has(id))continue;seen.add(id);
 const title=(a?.querySelector('strong')?.innerText||c.querySelector('.job-card-list__title,.job-card-list__title--link,.base-search-card__title,strong')?.innerText||a?.innerText||'').trim();
 links.push({portal_job_id:id,url:'https://www.linkedin.com/jobs/view/'+id+'/',title,
 employer:(c.querySelector('.artdeco-entity-lockup__subtitle,.base-search-card__subtitle')?.innerText||'').trim(),
 card_location:(c.querySelector('.job-card-container__metadata-item,.job-search-card__location')?.innerText||'').trim(),
 card_excerpt:(c.innerText||'').trim().slice(0,1200),posted_at:c.querySelector('time[datetime]')?.getAttribute('datetime')||null,
 posted_text:c.querySelector('time')?.innerText||null});}
 return {url:location.href,visible_cards:cards.length,links};}'''

def fetch(record,search,*,run_number=0,page_cap=1,query_cap=2,deadline=None,session_check_only=False):
    pages=[];leads=[];raw=[]
    with chrome(read_only=True) as c:
        c.route('**/*',readonly_route);p=c.pages[0] if c.pages else c.new_page();health=status(p)
        health.update(launcher='linkedin_browser.chrome',profile=str(__import__('linkedin_browser').PROFILE.resolve()),
                      read_only=True,service_workers='block')
        rid=os.environ.get('JOB_SEARCH_RUN_ID','diagnostic')
        (ROOT/'.workflow/runs'/f'{rid}-linkedin-session-health.json').write_text(json.dumps(health,indent=2)+'\n')
        if not health['signed_in']:
            rid=os.environ.get('JOB_SEARCH_RUN_ID','diagnostic')
            (ROOT/'.workflow/runs'/f'{rid}-linkedin-session-health.json').write_text(json.dumps(health,indent=2)+'\n')
            return {'source':'linkedin','status':'Blocked','error_kind':health.get('error_kind') or 'linkedin_selector_or_render_failure','session_health':health,'pages':[],
                    'leads':[],'checked_at':dt.datetime.now(dt.timezone.utc).isoformat()}
        if session_check_only:
            return {'source':'linkedin','status':'Success','session_health':health,'pages':[],
                    'leads':[],'checked_at':dt.datetime.now(dt.timezone.utc).isoformat()}
        for query,unused in _routes(record,search,run_number)[:query_cap or 2]:
            if deadline and time.monotonic()>deadline:break
            url=record['search_url'].format(query=quote(query),location='India',offset=0,page=1)
            p.goto(url,wait_until='domcontentloaded',timeout=30000);p.wait_for_timeout(3500)
            # The result list is virtualized; scroll only its own pane to expose this bounded page.
            p.evaluate("()=>{const e=document.querySelector('.jobs-search-results-list');if(e)e.scrollTop=500}");p.wait_for_timeout(1000)
            obs=p.evaluate(CARDS);excluded={};kept=0
            for lead in obs['links'][:25]:
                reason=card_filter_reason(lead)
                if reason=='no_relevant_title_or_generic_engineering_card_skills' and re.search(r'\bsoftware\s+(?:engineer|developer)\b|\bsde\b',lead['title'],re.I):
                    reason=None;lead['keyword_search_candidate']=True  # exact JD must still pass
                raw.append({**lead,'filter_reason':reason})
                if reason:excluded[reason]=excluded.get(reason,0)+1
                else:leads.append({**lead,'source':'linkedin','search_page_url':url});kept+=1
            pages.append({'query':query,'location':'India','url':url,'observed_url':obs['url'],
                'visible_cards':obs['visible_cards'],'lead_count':len(obs['links']),'retained_leads':kept,
                'card_filter_exclusions':excluded,'filters':{'easy_apply':True,'location':'India','experience_levels':['Entry level','Associate'],
                'posting_window':'last 7 days','sort':'newest'},'checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                'status':'Success' if obs['links'] else 'NoVisibleCards'})
    rid=os.environ.get('JOB_SEARCH_RUN_ID','diagnostic')
    (ROOT/'.workflow/runs'/f'{rid}-linkedin-search.json').write_text(json.dumps({'pages':pages,'cards':leads,'raw_cards':raw},indent=2)+'\n')
    return {'source':'linkedin','status':'Success' if pages and all(p['status']=='Success' for p in pages) else 'Partial',
            'pages':pages,'leads':leads,'checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),'error_kind':None}

def inspect(lead):
    with chrome(read_only=True) as c:
        c.route('**/*',readonly_route);p=c.pages[0] if c.pages else c.new_page()
        p.goto(details_url(lead['portal_job_id']),wait_until='domcontentloaded',timeout=30000);p.wait_for_timeout(3000)
        more=p.get_by_role('button',name=re.compile('Show more',re.I))
        if more.count()==1:more.click();p.wait_for_timeout(700)
        return p.evaluate(r'''()=>{
         const body=document.body.innerText;const title=document.querySelector('h1')?.innerText?.trim()||'';
         const company=document.querySelector('.job-details-jobs-unified-top-card__company-name')?.innerText?.trim()||'';
         const loc=document.querySelector('.job-details-jobs-unified-top-card__primary-description-container')?.innerText?.trim()||'';
         const jd=[...document.querySelectorAll('.jobs-description__content,.jobs-box__html-content,.jobs-description-content__text')].map(e=>e.innerText.trim()).sort((a,b)=>b.length-a.length)[0]||'';
         const easy=[...document.querySelectorAll('button.jobs-apply-button,button[aria-label*="Easy Apply to"]')].filter(b=>b.getClientRects().length&&/easy apply/i.test(b.innerText)&&!b.disabled);
         return {host:location.hostname,url:location.href,title,employer:company,location:loc,jd,body_characters:body.length,
          apply_visible:easy.length===1,unavailable:/no longer accepting applications/i.test(body),blocked:/captcha|security verification|account restricted/i.test(body),render_checks:1};}''')

def details_url(req):
    if not re.fullmatch(r'\d{8,}',str(req)):raise ValueError('Exact LinkedIn job ID required')
    return 'https://www.linkedin.com/jobs/search/?currentJobId='+str(req)+'&keywords=Java%20Backend&location=India'
