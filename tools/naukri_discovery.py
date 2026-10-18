"""Bounded Naukri search in the isolated direct Chrome session; no form actions."""
import datetime as dt
import time
import re
import json
from local_paths import ROOT
from urllib.parse import quote, urlsplit, parse_qs

from naukri_direct_playwright import _chrome, _status
from browser_portals import CARD_DOM, _routes

def readonly_route(route):
    request=route.request
    path=urlsplit(request.url).path.lower()
    if (any(x in path for x in ('/apply-workflow/','/botapi/')) or
            request.method not in ('GET','HEAD','OPTIONS')):
        route.abort()
    else:route.continue_()

def card_filter_reason(lead):
    """Discovery prefilter only; retained jobs still require full JD fit/dedupe gates."""
    title=lead.get('title') or ''
    if re.search(r'\b(?:bpo|kpo|data\s*entry|back[ -]*office|backend\s+(?:operations|process)|qa|quality|testing|tester|test|customer\s+(?:support|service))\b',title,re.I):
        return 'unrelated_bpo_data_entry_qa_role'
    match=re.search(r'\b(\d+(?:\.\d+)?)\s*[-–]\s*\d+(?:\.\d+)?\s*(?:Yrs|years)',lead.get('card_excerpt',''),re.I)
    if match and float(match.group(1))>=4:return 'experience_minimum_at_least_four'
    title_stack=bool(re.search(r'\bjava\b|spring|backend|back.end|\bapi\b',title,re.I))
    generic=bool(re.search(r'\bsoftware\s+(?:engineer|developer)\b|\bsde\b|\bfull[ -]?stack\s+(?:engineer|developer)\b',title,re.I))
    skill_text=' '.join(lead.get('card_skills') or [])+' '+(lead.get('card_excerpt') or '')
    card_stack=bool(re.search(r'\bjava\b|spring\s*boot|backend|back.end|microservices?|\brest\b',skill_text,re.I))
    if not (title_stack or generic and card_stack):return 'no_relevant_title_or_generic_engineering_card_skills'
    return None

def rendered_search_matches(query,location,state):
    normalize=lambda x:re.sub(r'[,\s]+',' ',str(x or '').lower()).strip()
    return normalize(state.get('keywords'))==normalize(query) and normalize(state.get('location'))==normalize(location)

def api_search_matches(query,location,experience,days,params):
    expected={'keyword':query.lower(),'location':location.lower(),'experience':str(experience),'jobAge':str(days),'sort':'f'}
    return bool(params) and all(params.get(k) and all(str(v).lower()==value for v in params[k]) for k,value in expected.items())

def fetch(record,search,*,run_number=0,page_cap=None,query_cap=None,deadline=None):
    pages=[];leads=[];raw_cards=[];error=None
    with _chrome(headless=False,read_only=True) as context:
        context.route('**/*',readonly_route)
        page=context.pages[0] if context.pages else context.new_page()
        search_requests=[]
        def capture_search(request):
            parsed=urlsplit(request.url)
            if parsed.hostname=='www.naukri.com' and parsed.path=='/jobapi/v3/search':
                params=parse_qs(parsed.query)
                search_requests.append({k:params[k] for k in ('keyword','location','experience','jobAge','sort') if k in params})
        page.on('request',capture_search)
        if not _status(page)['signed_in']:
            return {'source':'naukri','status':'Blocked','pages':[],'leads':[],
                    'error_kind':'isolated_session_signed_out','checked_at':dt.datetime.now(dt.timezone.utc).isoformat()}
        for query_index,(query,location) in enumerate(_routes(record,search,run_number)[:query_cap]):
            experience=2 if query_index%2==0 else 3
            location='India'  # explicit Naukri scope; confirmed willingness to work in any Indian city
            posting_days=15 if query_index==2 else 7  # fresh first, then one bounded fallback
            for number in range(1,min(record['max_pages'],page_cap or record['max_pages'])+1):
                if deadline and time.monotonic()>deadline:error='run_deadline';break
                url=record['search_url'].format(query=quote(query),location=quote(location),page=number,offset=0)
                page.goto(url,wait_until='domcontentloaded',timeout=30000)
                page.locator('#filter-freshness').wait_for(state='visible',timeout=15000)
                page.locator('#filter-freshness').click(timeout=10000)
                page.locator('a[data-id="filter-freshness-'+str(posting_days)+'"]').click(timeout=10000)
                page.wait_for_timeout(1200)
                page.locator('#filter-sort').click(timeout=10000)
                page.locator('a[data-id="filter-sort-f"]').click(timeout=10000)
                page.wait_for_timeout(1200)
                slider=page.locator('[data-filter-id="experience"] .rc-slider')
                box=slider.bounding_box()
                if not box:raise RuntimeError('Experience slider unavailable')
                handle=page.locator('[data-filter-id="experience"] .cir').bounding_box()
                page.mouse.move(handle['x']+handle['width']/2,handle['y']+handle['height']/2)
                page.mouse.down();page.mouse.move(box['x']+box['width']*experience/30,box['y']+box['height']/2,steps=8);page.mouse.up()
                page.wait_for_timeout(1600)
                exp=page.locator('[data-filter-id="experience"] .handle span').inner_text()
                filters={'experience_label':exp,'freshness_label':page.locator('#filter-freshness').inner_text(),
                         'sort_label':page.locator('#filter-sort').inner_text()}
                search_state=page.evaluate('''()=>({keywords:document.querySelector('input[placeholder*="keyword"]')?.value,
                    location:document.querySelector('input[placeholder="Enter location"]')?.value,
                    experience:document.querySelector('#experienceDD')?.value,heading:document.querySelector('h1')?.innerText})''')
                if not rendered_search_matches(query,location,search_state):raise RuntimeError('Rendered search keywords/location do not match requested scope')
                if not search_requests or not api_search_matches(query,location,experience,posting_days,search_requests[-1]):
                    raise RuntimeError('Native search API keywords/location/experience/date filters did not verify')
                if not re.search(r'^'+str(experience)+r'(?:\s*(?:yrs?|years?))?$',exp,re.I) or str(posting_days) not in filters['freshness_label'] or filters['sort_label']!='Date':
                    raise RuntimeError('Native Naukri search filters did not verify')
                # Filter changes return to page one; use the native Next action
                # afterward to inspect a second page, retaining the selected filters.
                for _ in range(number-1):
                    page.get_by_text('Next',exact=True).click(timeout=10000);page.wait_for_timeout(2000)
                obs=page.evaluate(CARD_DOM['naukri'])
                for _ in range(2):
                    if obs.get('visible_cards') or obs.get('blocked'):break
                    page.wait_for_timeout(1800);obs=page.evaluate(CARD_DOM['naukri'])
                stamp=dt.datetime.now(dt.timezone.utc).isoformat()
                status='Blocked' if obs.get('blocked') else 'Success' if obs.get('links') else 'NoVisibleCards'
                if urlsplit(obs.get('url','')).hostname!='www.naukri.com':status='Blocked'
                pages.append({'query':query,'location':location,'page':number,'url':url,'observed_url':obs.get('url'),
                    'checked_at':stamp,'status':status,'visible_cards':obs.get('visible_cards'),
                    'lead_count':len(obs.get('links',[])), 'filters':filters,'rendered_search':search_state,
                    'last_search_api_parameters':search_requests[-1] if search_requests else None,
                    'error_kind':None if status=='Success' else status})
                rejected={};retained=0
                for lead in obs.get('links',[]):
                    reason=card_filter_reason(lead)
                    raw_cards.append({**lead,'source':'naukri','search_page_url':url,'filter_reason':reason})
                    if reason:
                        rejected[reason]=rejected.get(reason,0)+1;continue
                    retained+=1
                    leads.append({**lead,'source':'naukri','search_page_url':url})
                pages[-1].update(extracted_exact_ids=[x.get('portal_job_id') for x in obs.get('links',[])],
                                 retained_leads=retained,card_filter_exclusions=rejected)
                if status!='Success':error=status;break
    import os
    run_id=os.environ.get('JOB_SEARCH_RUN_ID','diagnostic')
    (ROOT/'.workflow/runs'/f'{run_id}-naukri-search.json').write_text(json.dumps({'pages':pages,'cards':leads,'raw_cards':raw_cards},indent=2)+'\n')
    return {'source':'naukri','status':'Success' if pages and all(x['status']=='Success' for x in pages) else 'Partial',
            'pages':pages,'leads':leads,'error_kind':error,'fetch_succeeded':bool(leads),
            'checked_at':dt.datetime.now(dt.timezone.utc).isoformat()}

def inspect(lead):
    from portal_native import DETAIL_DOM
    with _chrome(headless=False,read_only=True) as context:
        context.route('**/*',readonly_route)
        page=context.pages[0] if context.pages else context.new_page()
        page.goto(lead['url'],wait_until='domcontentloaded',timeout=30000)
        page.wait_for_selector('h1',state='visible',timeout=15000)
        page.wait_for_timeout(1800)
        obs=page.evaluate(DETAIL_DOM);obs['render_checks']=1
        return obs
