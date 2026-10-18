"""Bounded read-only preflight for the explicitly authorized Tech Mahindra pilot."""
import datetime as dt
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0,'tools')
from naukri_direct_playwright import ROOT,_chrome,_status
from job_db import connect

JOB_ID=244
REQ='070926003879'
OUT=ROOT/'jobs/naukri-070926003879/preflight-20260929.json'

def now():return dt.datetime.now(dt.timezone.utc).isoformat()

def main():
    phase='sqlite_preflight';globals()['PHASE']=phase
    db=connect()
    try:
        job=db.execute('select * from jobs where id=?',(JOB_ID,)).fetchone()
        if not job or job['requisition_id']!=REQ or job['state'] not in ('ResumeReady','InProgress') or job['fit']!='Medium':
            raise RuntimeError('Exact staged job/state gate failed')
        if db.execute('select 1 from submission_attempts where job_id=?',(JOB_ID,)).fetchone():
            raise RuntimeError('Existing attempt blocks this job')
        if db.execute('select 1 from history_keys where employer_key=? and requisition_key=?',(job['employer_key'],job['requisition_key'])).fetchone():
            raise RuntimeError('History key blocks this job')
        url=job['canonical_url']
    finally:db.close()
    result={'job_id':JOB_ID,'requisition_id':REQ,'canonical_url':url,'checked_at':now(),
            'signed_in':False,'account_name_matches':False,'history':{},'listing':{},
            'application_form_opened':False,'application_action_clicked':False,'submission_attempted':False}
    phase='isolated_chrome_launch';globals()['PHASE']=phase
    with _chrome(headless=False) as context:
        page=context.pages[0] if context.pages else context.new_page()
        blocked=[]
        def guard(route):
            request=route.request;path=urlsplit(request.url).path.lower()
            if request.method not in ('GET','HEAD','OPTIONS') and any(x in path for x in ('/apply-workflow/','/botapi/','/upload','/resume','/submit')):
                blocked.append({'method':request.method,'path':path});route.abort()
            else:route.continue_()
        context.route('**/*',guard)
        phase='account_check';globals()['PHASE']=phase
        status=_status(page)
        if not status['signed_in']:raise RuntimeError('Isolated Naukri session is signed out')
        result['signed_in']=True
        result['account_name_matches']=bool(page.evaluate("() => /Yugal\\s+Teotia/i.test(document.body.innerText)"))
        if not result['account_name_matches']:raise RuntimeError('Signed-in account identity mismatch')
        phase='applied_history_navigation';globals()['PHASE']=phase
        page.goto('https://www.naukri.com/myapply/historypage',wait_until='domcontentloaded',timeout=30000)
        page.wait_for_timeout(1200)
        source=page.locator('.apply-filters li').filter(has_text='Applies on Naukri')
        if source.count()!=1:raise RuntimeError('Naukri Applied Jobs source filter unavailable')
        source.click();page.wait_for_timeout(1400)
        sweeps=[];target=None
        phase='applied_history_pagination';globals()['PHASE']=phase
        for _ in range(15):
            snap=page.evaluate('''req=>{const rows=[...document.querySelectorAll('.jdTuplesContainer .jdTupleContainer')];
              const labels=[...document.querySelectorAll('.apply-filters li')].map(x=>x.innerText.trim());
              const matches=rows.filter(x=>x.outerHTML.includes(req)||(/Tech Mahindra/i.test(x.innerText)&&/Java Backend Developer/i.test(x.innerText)));
              const selected=labels.find(x=>x.includes('Applies on Naukri'))||'';const totalText=(selected.split('(')[1]||'').split(')')[0].trim();const total=/^\\d+$/.test(totalText)?Number(totalText):null;
              const more=document.querySelector('#loadMoreLnk');return {loaded_rows:rows.length,source_total:total,
                exact_id_matches:rows.filter(x=>x.outerHTML.includes(req)).length,
                company_role_matches:matches.length,matching_rows:matches.map(x=>x.innerText.trim().slice(0,200)),
                more_visible:!!more&&!!more.getClientRects().length};}''',REQ)
            sweeps.append(snap);target=snap['source_total']
            if snap['exact_id_matches'] or snap['company_role_matches'] or (target is not None and snap['loaded_rows']>=target) or not snap['more_visible']:break
            more=page.locator('#loadMoreLnk');more.scroll_into_view_if_needed(timeout=5000);more.click(timeout=5000,force=True);page.wait_for_timeout(900)
        final=sweeps[-1]
        result['history']={'checked_at':now(),'rows_loaded':final['loaded_rows'],'source_total':target,
            'full_history_loaded':target is not None and final['loaded_rows']>=target,
            'exact_id_matches':final['exact_id_matches'],'company_role_matches':final['company_role_matches'],
            'matching_rows':final['matching_rows'],'sweeps':[x['loaded_rows'] for x in sweeps]}
        if not result['history']['full_history_loaded'] or final['exact_id_matches'] or final['company_role_matches']:
            result['blocked_application_requests']=blocked
            OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
            raise RuntimeError('Full history did not clear exact job/company-role deduplication')
        phase='exact_listing_check';globals()['PHASE']=phase
        page.goto(url,wait_until='domcontentloaded',timeout=30000);page.wait_for_timeout(1100)
        listing=page.evaluate('''req=>{const b=[...document.querySelectorAll('#apply-button')].filter(x=>x.getClientRects().length);
          const t=document.body.innerText;return {exact_path:location.pathname.endsWith(req),title:document.title,
            apply_visible:b.some(x=>x.innerText.trim()==='Apply'),applied_button:b.some(x=>/Applied/i.test(x.innerText)),
            applied_status:/(^|\\n)Applied(\\n|$)/.test(t),pending:/Continue application|Pending application/i.test(t)};}''',REQ)
        result['listing']=listing;result['listing_checked_at']=now()
        result['blocked_application_requests']=blocked
        if not listing['exact_path'] or not listing['apply_visible'] or listing['applied_button'] or listing['applied_status'] or listing['pending']:
            raise RuntimeError('Exact listing is not live and unapplied')
    OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'evidence_path':str(OUT.relative_to(ROOT)),'job_id':JOB_ID,'requisition_id':REQ,
        'signed_in':result['signed_in'],'account_name_matches':result['account_name_matches'],
        'history':result['history'],'listing':result['listing'],'submission_attempted':False},ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except Exception as e:
        print(json.dumps({'phase':globals().get('PHASE','unknown'),'error_type':type(e).__name__,'error':str(e) if isinstance(e,RuntimeError) else 'Preflight failed'}))
        raise SystemExit(1)
