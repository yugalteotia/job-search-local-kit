"""Bounded, read-only scan of the full Naukri Applied Jobs list for one job."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from urllib.parse import urlsplit

from naukri_direct_playwright import ROOT, _chrome, _status

REQ = "190826038395"
OUT = ROOT / "jobs/naukri-190826038395/full-applied-history-audit.json"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def snapshot(page):
    return page.evaluate("""req => {
      const rows=[...document.querySelectorAll('.jdTuplesContainer .jdTupleContainer')];
      const matches=rows.filter(e=>e.outerHTML.includes(req)||
        (/Infosys/i.test(e.innerText)&&/Java Backend Developer/i.test(e.innerText)));
      const filter=[...document.querySelectorAll('.apply-filters li')].map(e=>e.innerText.trim());
      const more=document.querySelector('#loadMoreLnk');
      return {loadedRows:rows.length,sourceFilterLabels:filter,
        exactIdMatches:rows.filter(e=>e.outerHTML.includes(req)).length,
        companyRoleMatches:matches.length,
        matchingRows:matches.map(e=>({text:e.innerText.trim().slice(0,500),exactIdInMarkup:e.outerHTML.includes(req)})),
        loadMorePresent:!!more,loadMoreVisible:!!more&&!!more.getClientRects().length,
        listScrollTop:document.querySelector('.jdTuplesContainer')?.scrollTop||0};
    }""",REQ)


def main():
    blocked=[]
    started=now()
    with _chrome(headless=False) as context:
        page=context.pages[0] if context.pages else context.new_page()
        def guard(route):
            req=route.request
            path=urlsplit(req.url).path.lower()
            if req.method not in ('GET','HEAD','OPTIONS') and any(x in path for x in
                ('/apply-workflow/','/botapi/','/upload','/resume','/submit')):
                blocked.append({'method':req.method,'path':path});route.abort()
            else:route.continue_()
        context.route('**/*',guard)
        if not _status(page)['signed_in']:
            raise RuntimeError('Isolated Naukri session is not signed in')
        page.goto('https://www.naukri.com/myapply/historypage',wait_until='domcontentloaded',timeout=30000)
        page.wait_for_timeout(1800)
        source=page.locator('.apply-filters li').filter(has_text='Applies on Naukri')
        if source.count()!=1:
            raise RuntimeError('Could not identify the Naukri Applied Jobs source filter')
        source.click()
        page.wait_for_timeout(1400)
        sweeps=[]
        unchanged=0
        for index in range(12):
            current=snapshot(page)
            current['checked_at']=now()
            sweeps.append(current)
            if current['exactIdMatches'] or current['companyRoleMatches']:
                break
            if current['loadedRows']>=74 or not current['loadMorePresent']:
                break
            before=current['loadedRows']
            more=page.locator('#loadMoreLnk')
            more.scroll_into_view_if_needed(timeout=5000)
            more.click(timeout=5000,force=True)
            page.wait_for_timeout(1400)
            after=snapshot(page)['loadedRows']
            unchanged=unchanged+1 if after<=before else 0
            if unchanged>=2:
                break
        result={'started_at':started,'finished_at':now(),'job_id':220,'requisition_id':REQ,
                'history_url':'https://www.naukri.com/myapply/historypage',
                'source_filter':'Applies on Naukri','expected_source_count':74,
                'sweeps':sweeps,'final':sweeps[-1],
                'full_source_count_loaded':sweeps[-1]['loadedRows']>=74,
                'application_mutation_requests_blocked':blocked,
                'apply_or_answer_controls_clicked':False}
    OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'evidence_path':str(OUT.relative_to(ROOT)),
                      'sha256':hashlib.sha256(OUT.read_bytes()).hexdigest(),
                      'finished_at':result['finished_at'],
                      'rows_per_sweep':[x['loadedRows'] for x in sweeps],
                      'final':result['final'],
                      'full_source_count_loaded':result['full_source_count_loaded'],
                      'blocked_writes':blocked},ensure_ascii=False))

if __name__=='__main__':main()
