"""Allowlisted, read-only portal page coverage through private Playwright MCP."""
import datetime as dt
import json
import re
import time
from urllib.parse import quote,urlsplit

from parallel_browser_probe import MCP
from tab_lifecycle import OwnedTabs

def parse_json_result(result):
    value=' '.join(x.get('text','') for x in result.get('content',[]) if x.get('type')=='text')
    for index,char in enumerate(value):
        if char!='{':continue
        try:
            found,_=json.JSONDecoder().raw_decode(value[index:])
            if isinstance(found,dict) and 'host' in found:return found
        except ValueError:continue
    raise ValueError('Browser DOM check did not return structured coverage')

DOM=r'''() => {
  const links=[...document.querySelectorAll('a[href]')].filter(a=>{
    const h=(a.href||'').toLowerCase(); const t=(a.innerText||'').trim();
    return t.length>=10 && t.length<150 && /(job|career|lever|greenhouse|ashby)/.test(h);
  }).slice(0,30).map(a=>{const u=new URL(a.href);let url=u.origin+u.pathname;
    if(u.hostname==='in.indeed.com' && u.pathname==='/viewjob' && /^[a-f0-9]{12,24}$/i.test(u.searchParams.get('jk')||'')) url+='?jk='+u.searchParams.get('jk');
    const card=a.closest('li[data-occludable-job-id],li[data-job-id],article[data-job-id],.job-card-container,.jobTuple,article[class*=job],li[class*=job]');
    const cardText=(card?.innerText||'').trim().slice(0,700);
    const loc=[...(card?.querySelectorAll('[class*=location],[data-testid*=location],[class*=metadata-wrapper]')||[])].map(e=>(e.innerText||'').trim()).find(v=>v.length<120&&/\b(?:India|United States|Pune|Bengaluru|Bangalore|Hyderabad|Delhi|Noida|Gurugram|Mumbai|Chennai)\b/i.test(v))||null;
    return {title:(a.innerText||'').trim().slice(0,120),url,card_location:loc,card_excerpt:cardText};});
  const body=(document.body?.innerText||'').slice(0,2000).toLowerCase();
  return {host:location.hostname,url:location.origin+location.pathname,title:document.title,
    blocked:/additional verification required|access denied|captcha|verify you are human|unusual traffic/.test(body),
    links};
}'''

# Search result markup differs substantially across these portals. Keep the
# selectors tied to visible cards; a category/search link is never a job lead.
CARD_DOM={
 'naukri':r'''() => {
   const selector='.srp-jobtuple-wrapper,.cust-job-tuple,.jobTuple,article.jobTuple';
   const cards=[...document.querySelectorAll(selector)].filter(c=>!c.parentElement?.closest(selector));
   const seen=new Set(),links=[];
   for(const card of cards){
     const a=[...card.querySelectorAll('a[href]')].find(a=>/\/job-listings-[^/?]*-\d{8,}\/?(?:\?|$)/i.test(a.href));
     if(!a)continue;
     const u=new URL(a.href),id=(u.pathname.match(/-(\d{8,})\/?$/)||[])[1];
     if(!id||seen.has(id))continue;seen.add(id);
     links.push({title:(a.innerText||a.getAttribute('title')||'').trim().slice(0,140),url:u.origin+u.pathname,portal_job_id:id,
       employer:(card.querySelector('.comp-name,.companyInfo')?.innerText||'').trim().slice(0,120),
       card_skills:[...new Set([...card.querySelectorAll('.tags-gt li,.tag-li')].map(x=>(x.innerText||'').trim()).filter(Boolean))],
       card_location:(card.querySelector('.locWdth,.location')?.innerText||'').trim().slice(0,120)||null,
       posted_at:card.querySelector('time[datetime],[itemprop="datePosted"][datetime]')?.getAttribute('datetime')||null,
       posted_text:(card.querySelector('[class*=posted],time')?.innerText||'').trim().slice(0,80)||(card.innerText||'').match(/(?:\d+\s*(?:days?|weeks?|months?|hours?)\s+ago|Just now|Today)/i)?.[0]||null,
       card_excerpt:(card.innerText||'').trim().slice(0,700)});
   }
   const body=(document.body?.innerText||'').slice(0,2000).toLowerCase();
   return {host:location.hostname,url:location.href,title:document.title,visible_cards:cards.length,
     body_characters:(document.body?.innerText||'').length,
     blocked:/additional verification required|access denied|captcha|verify you are human|unusual traffic/.test(body),links:links.slice(0,30)};
 }''',
 'cutshort':r'''() => {
   const anchors=[...document.querySelectorAll('a[href]')].filter(a=>{
     try{const u=new URL(a.href);return u.hostname==='cutshort.io'&&/^\/job\/[^/]+-[A-Za-z0-9]{8}\/?$/.test(u.pathname)&&!!a.closest('h2');}catch{return false}
   });
   const seen=new Set(),links=[];
   for(const a of anchors){const u=new URL(a.href),id=(u.pathname.match(/-([A-Za-z0-9]{8})\/?$/)||[])[1];
     if(!id||seen.has(id))continue;seen.add(id);
     const card=a.closest('article,[class*=jobCard],[class*=job-card]')||a.parentElement?.parentElement?.parentElement;
     links.push({title:(a.innerText||'').trim().slice(0,140),url:u.origin+u.pathname,portal_job_id:id,
       card_location:(card?.querySelector('[class*=location]')?.innerText||'').trim().slice(0,120)||null,
       posted_at:card?.querySelector('time[datetime],[itemprop="datePosted"][datetime]')?.getAttribute('datetime')||null,
       posted_text:(card?.innerText||'').match(/Posted\s+[^\n]{3,45}/i)?.[0]||null,
       card_excerpt:(card?.innerText||'').trim().slice(0,700)});
   }
   const body=(document.body?.innerText||'').slice(0,2000).toLowerCase();
   return {host:location.hostname,url:location.href,title:document.title,visible_cards:anchors.length,
     body_characters:(document.body?.innerText||'').length,
     blocked:/additional verification required|access denied|captcha|verify you are human|unusual traffic/.test(body),links:links.slice(0,60)};
 }''',
 'foundit':r'''() => {
   const cards=[...document.querySelectorAll('.cardContainer[id]')].filter(c=>/^\d{5,}$/.test(c.id)&&!!c.querySelector('#jobCardTitle'));
   const body=(document.body?.innerText||'').slice(0,2000).toLowerCase();
   return {host:location.hostname,url:location.href,title:document.title,visible_cards:cards.length,
     body_characters:(document.body?.innerText||'').length,
     blocked:/additional verification required|access denied|captcha|verify you are human|unusual traffic/.test(body),
     cards:cards.slice(0,20).map(c=>({portal_job_id:c.id,title:(c.querySelector('#jobCardTitle')?.innerText||'').trim().slice(0,140),
       employer:(c.querySelector('.companyName')?.innerText||'').trim().slice(0,120),
       card_location:(c.querySelector('.location')?.innerText||'').trim().slice(0,120)||null,
       posted_at:c.querySelector('time[datetime],[itemprop="datePosted"][datetime]')?.getAttribute('datetime')||null,
       posted_text:(c.querySelector('.jobAddedTime,.timeText')?.innerText||'').trim().slice(0,80)||null,
       card_excerpt:(c.innerText||'').trim().slice(0,700)})),links:[]};
 }'''
}

FOUNDIT_SELECTED=r'''() => {
  const active=document.querySelector('.cardContainer.activeCard[id]');
  const links=[...document.querySelectorAll('a[href]')].filter(a=>{
    try{const u=new URL(a.href);return u.protocol==='https:'&&u.hostname!==location.hostname&&
      /(?:\/job\/[^/?]+|\/jobs\/[^/?]+|\/job-listings-[^/?]+|myworkdayjobs\.com\/.+\/job\/[^/?]+)/i.test(u.href)}catch{return false}
  }).map(a=>({url:a.href,title:(a.innerText||'').trim().slice(0,120)}));
  return {host:location.hostname,active_card_id:active?.id||null,links:links.slice(0,10)};
}'''

def _routes(config,search,run_number):
    terms=search['terms'];locations=search['locations']
    count=config['max_queries'];pairs=[]
    for i in range(count):
        term=terms[(run_number+i)%len(terms)]
        location=locations[(run_number+i)%len(locations)]
        pairs.append((term,location))
    return pairs

def fetch_portal(name,config,search,*,run_number=0,page_cap=None,query_cap=None,deadline=None,client_factory=MCP):
    if not config['enabled']:return {'source':name,'status':'Disabled','pages':[],'leads':[],'error_kind':None,'checked_at':dt.datetime.now(dt.timezone.utc).isoformat()}
    requested=config['max_pages']*config['max_queries']
    pages=[];leads=[];error=None;client=None;blocked=False;owned=None;tab_id=None;lifecycle={}
    cap=min(config['max_pages'],page_cap or config['max_pages'])
    try:
        client=client_factory();owned=OwnedTabs(client, owner=f"portal:{name}")
        for term,location in _routes(config,search,run_number)[:query_cap]:
            for page in range(1,cap+1):
                if deadline and time.monotonic()>deadline:
                    error='run_deadline';break
                limit=lambda seconds:max(1,min(seconds,deadline-time.monotonic())) if deadline else seconds
                url=config['search_url'].format(query=quote(term),location=quote(location),page=page,offset=(page-1)*25)
                if urlsplit(url).scheme!='https' or urlsplit(url).hostname!=config['host']:
                    raise ValueError('Portal route escaped configured allowlist')
                if tab_id is None:tab_id=owned.open(url)
                else:
                    client.tool('browser_navigate',{'url':url},timeout=limit(40));owned.note_reuse(tab_id)
                observation=parse_json_result(client.tool('browser_evaluate',{'function':CARD_DOM.get(name,DOM)},timeout=limit(30)))
                checks=1
                while name in CARD_DOM and not observation.get('blocked') and observation.get('visible_cards')==0 and checks<3:
                    time.sleep(min(2,limit(2)))
                    observation=parse_json_result(client.tool('browser_evaluate',{'function':CARD_DOM[name]},timeout=limit(30)))
                    checks+=1
                stamp=dt.datetime.now(dt.timezone.utc).isoformat()
                if observation.get('host')!=config['host']:
                    pages.append({'query':term,'location':location,'page':page,'url':url,'checked_at':stamp,'status':'Failed','lead_count':0,'error_kind':'redirected_outside_allowlist'})
                    error='redirected_outside_allowlist';blocked=True;break
                if observation.get('blocked'):
                    pages.append({'query':term,'location':location,'page':page,'url':url,'checked_at':stamp,'status':'Blocked','lead_count':0,'error_kind':'site_verification'})
                    error='site_verification';blocked=True;break
                source_links=[{**x,'source':name,'search_page_url':url} for x in observation.get('links',[])
                              if isinstance(x,dict) and x.get('url') and x.get('title')]
                unresolved=[]
                if name=='foundit':
                    for card in observation.get('cards',[])[:10]:
                        card_id=card.get('portal_job_id','')
                        if not re.fullmatch(r'\d{5,}',card_id):continue
                        try:
                            client.tool('browser_click',{'target':f'.cardContainer[id="{card_id}"] #jobCardTitle'},timeout=limit(20))
                            selected=parse_json_result(client.tool('browser_evaluate',{'function':FOUNDIT_SELECTED},timeout=limit(20)))
                            if selected.get('active_card_id')!=card_id:
                                unresolved.append({'portal_job_id':card_id,'reason':'selected_card_id_mismatch'});continue
                            exact=next((x for x in selected.get('links',[]) if x.get('url')),None)
                            if exact:
                                source_links.append({**card,'url':exact['url'],'source':'foundit',
                                                     'search_page_url':url,'provenance':'selected_card_visible_employer_link'})
                            else:unresolved.append({'portal_job_id':card_id,'reason':'no_job_specific_url_in_selected_detail'})
                        except (RuntimeError,TimeoutError,ValueError) as exc:
                            unresolved.append({'portal_job_id':card_id,'reason':type(exc).__name__,
                                               'detail':str(exc)[:240]})
                leads.extend(source_links)
                visible=observation.get('visible_cards')
                no_cards=visible==0 if visible is not None else False
                blank_page=no_cards and (observation.get('body_characters') or 0)<50
                no_exact_links=bool(visible) and not source_links
                pages.append({'query':term,'location':location,'page':page,'url':url,'checked_at':stamp,
                              'status':'BlankPage' if blank_page else 'NoVisibleCards' if no_cards else
                                       'CardsWithoutExactLinks' if no_exact_links else 'Success',
                              'error_kind':'job_specific_url_not_visible' if no_exact_links else None,
                              'visible_cards':visible,'body_characters':observation.get('body_characters'),
                              'render_checks':checks,'lead_count':len(source_links),
                              'card_ids':list(dict.fromkeys(x.get('portal_job_id') for x in
                                  (observation.get('cards') or source_links) if x.get('portal_job_id'))),
                              'unresolved_cards':unresolved,
                              'observed_url':observation.get('url'),'sort':'Configured newest route where available'})
            if blocked:break
            if error=='run_deadline':break
    except (RuntimeError,TimeoutError,ValueError,OSError) as exc:
        error=type(exc).__name__ if not error else error
    finally:
        if client:
            if owned:lifecycle.update(owned.close_all())
            client.close()
    success=sum(p['status']=='Success' for p in pages)
    if blocked:status='Blocked' if not success else 'Partial'
    elif success==requested and not error:status='Success'
    elif success:status='Partial'
    elif pages and all(p['status']=='NoVisibleCards' for p in pages):status='NoVisibleCards'
    elif pages and all(p['status']=='BlankPage' for p in pages):status='BlankPage'
    elif pages and all(p['status']=='CardsWithoutExactLinks' for p in pages):status='CardsWithoutExactLinks'
    else:status='Failed'
    return {'source':name,'status':status,'pages':pages,'leads':leads,'error_kind':error,
            'checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),'requested_pages':requested,
            'successful_pages':success,'fetch_succeeded':bool(success),'tab_lifecycle':lifecycle}
