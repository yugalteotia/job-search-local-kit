"""Bounded, read-only verification of the three card-based portal adapters."""
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import browser_portals
import job_config
import lead_resolution
import review_candidates
from parallel_browser_probe import MCP
from tab_lifecycle import OwnedTabs

ROOT=Path(__file__).resolve().parents[1]
SOURCES=('naukri','foundit','cutshort')
DETAIL_DOM=r'''() => {
  const body=document.body?.innerText||'';
  const selectors=['[data-automation-id="jobPostingDescription"]','#jobDescriptionText',
    '[class*="job-desc"]','[class*="jobDescription"]','[class*="job-description"]',
    '[class*="description__text"]','[class*="jobDetails"]'];
  let picked=null;
  if(location.hostname==='cutshort.io'){
    const start=body.indexOf('\nSkills\n');
    const end=start>=0?body.indexOf('\nBrowse related jobs',start):-1;
    if(start>=0&&end>start)picked={text:body.slice(start,end),selector:'cutshort_exact_job_before_related'};
  }
  for(const selector of selectors){if(picked)break;const candidates=[...document.querySelectorAll(selector)]
    .map(e=>({text:(e.innerText||'').trim(),selector})).filter(x=>x.text.length>=250);
    if(candidates.length){picked=candidates.sort((a,b)=>b.text.length-a.text.length)[0];break}}
  if(!picked){const start=body.search(/\b(?:Job Description|About the job)\b/i);
    if(start>=0)picked={text:body.slice(start,start+14000),selector:'visible_job_description_section'}}
  return {host:location.hostname,url:location.href,title:document.title,
    body_characters:body.length,
    blocked:/access denied|captcha|verify you are human|unusual traffic|403 forbidden/i.test(body.slice(0,1800)),
    jd:(picked?.text||'').slice(0,14000),jd_selector:picked?.selector||null};
}'''

def inspect(lead,source):
    url=lead['url']
    client=None;owned=None
    try:
        client=MCP();owned=OwnedTabs(client,owner=f'three-portal-detail:{source}')
        owned.open(url)
        data=browser_portals.parse_json_result(client.tool('browser_evaluate',{'function':DETAIL_DOM},timeout=30))
        checks=1
        while (data.get('body_characters') or 0)<50 and checks<3:
            time.sleep(2)
            data=browser_portals.parse_json_result(client.tool('browser_evaluate',{'function':DETAIL_DOM},timeout=30))
            checks+=1
        expected=urlsplit(url)
        observed=urlsplit(data.get('url',''))
        exact=(expected.hostname==observed.hostname and expected.path.rstrip('/')==observed.path.rstrip('/'))
        text=data.get('jd') or ''
        title_match=lead_resolution._role_similarity(lead.get('title'),data.get('title'))>=0.5
        blank=(data.get('body_characters') or 0)<50
        return {'url':url,'observed_url':data.get('url'),'status':'Blocked' if data.get('blocked') else
                'BlankPage' if blank else 'ExactDetail' if exact else 'Redirected',
                'render_checks':checks,'body_characters':data.get('body_characters'),
                'jd_selector':data.get('jd_selector'),
                'title_match':title_match,'jd_characters':len(text),'substantive_jd':bool(exact and title_match and not data.get('blocked') and
                    review_candidates.substantive_jd(text)),
                'jd_excerpt':text[:550] if exact else None}
    except (RuntimeError,TimeoutError,ValueError,OSError) as exc:
        return {'url':url,'status':'Failed','error_kind':type(exc).__name__,'substantive_jd':False}
    finally:
        if client:
            if owned:owned.close_all()
            client.close()

def run():
    cfg=job_config.load()
    if cfg['mode']!='read_only' or cfg['submissions']['enabled']:
        raise ValueError('Read-only discovery mode is required')
    output={'started_at':dt.datetime.now(dt.timezone.utc).isoformat(),'mode':'read_only',
            'forms_opened':0,'uploads':0,'submissions':0,'sources':{}}
    names=tuple(sys.argv[1:]) or SOURCES
    if any(name not in SOURCES for name in names):raise ValueError('Only Naukri, Foundit, and Cutshort may be verified')
    search=dict(cfg['search'],terms=['java spring boot'],locations=['India'])
    for name in names:
        record=cfg['sources'][name]
        result=browser_portals.fetch_portal(name,record,search,page_cap=1,query_cap=1)
        leads=list({x['url']:x for x in result.get('leads',[]) if x.get('url')}.values())
        ids=set(x.get('portal_job_id') for x in leads if x.get('portal_job_id'))
        for page in result.get('pages',[]):ids.update(page.get('card_ids') or [])
        ranked=sorted(leads,key=lambda x:(bool(re.search(r'java|spring|backend|api',x.get('title',''),re.I)),
                                          bool(x.get('card_location'))),reverse=True)
        details=[inspect(x,name) for x in ranked[:3]]
        output['sources'][name]={'status':result['status'],'error_kind':result.get('error_kind'),
           'pages':result.get('pages',[]),'visible_cards':sum(p.get('visible_cards') or 0 for p in result.get('pages',[])),
           'extracted_job_ids':len(ids),'extracted_exact_urls':len(leads),'job_ids':sorted(ids),
           'leads':leads,'detail_pages_checked':len(details),
           'substantive_jd_pages':sum(x['substantive_jd'] for x in details),
           'details':details,'tab_lifecycle':result.get('tab_lifecycle')}
    output['finished_at']=dt.datetime.now(dt.timezone.utc).isoformat()
    path=ROOT/'.workflow'/'reviews'/(dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')+'-three-portal-verification.json')
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(output,indent=2,ensure_ascii=False)+'\n')
    return path,output

if __name__=='__main__':
    path,output=run()
    print(json.dumps({'report':str(path),'sources':{name:{k:v for k,v in result.items() if k in
      ('status','error_kind','visible_cards','extracted_job_ids','extracted_exact_urls','detail_pages_checked','substantive_jd_pages')}
      for name,result in output['sources'].items()}},indent=2))
