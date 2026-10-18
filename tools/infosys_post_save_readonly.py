"""Read-only listing/profile check after the one claimed Infosys chat Save."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
from urllib.parse import urlsplit
from naukri_direct_playwright import ROOT,PROFILE_URL,_chrome,_status

REQ='190826038395'
URL='https://www.naukri.com/job-listings-java-backend-developer-spring-boot-microservices-infosys-pune-chennai-bengaluru-2-to-5-years-190826038395'
OUT=ROOT/'jobs/naukri-190826038395/post-save-listing-audit.json'
def now():return dt.datetime.now(dt.timezone.utc).isoformat()

def main():
    blocked=[]
    with _chrome(headless=False) as c:
        p=c.pages[0] if c.pages else c.new_page()
        def guard(route):
            q=route.request;path=urlsplit(q.url).path.lower()
            if q.method not in ('GET','HEAD','OPTIONS') and any(x in path for x in ('/apply-workflow/','/botapi/','/submit','/upload','/resume')):
                blocked.append({'method':q.method,'path':path});route.abort()
            else:route.continue_()
        c.route('**/*',guard)
        if not _status(p)['signed_in']:raise RuntimeError('Isolated Naukri session signed out')
        p.goto(URL,wait_until='domcontentloaded',timeout=30000)
        p.wait_for_timeout(1800)
        listing=p.evaluate("""req=>{
          const buttons=[...document.querySelectorAll('#apply-button')].filter(e=>e.getClientRects().length);
          const text=document.body.innerText;
          return {path:location.pathname,exactIdInPath:location.pathname.endsWith(req),
            applyVisible:buttons.some(e=>e.innerText.trim()==='Apply'),
            appliedButtonVisible:buttons.some(e=>/Applied/i.test(e.innerText)),
            appliedStatusVisible:/(^|\\n)Applied(\\n|$)/.test(text),
            pendingVisible:/Continue application|Pending application/i.test(text),
            chatDrawerVisible:!![...document.querySelectorAll('.chatbot_Drawer')].find(e=>e.getClientRects().length)}
        }""",REQ)
        listing['checked_at']=now()
        p.goto(PROFILE_URL,wait_until='domcontentloaded',timeout=30000)
        p.wait_for_timeout(1200)
        profile=p.evaluate("""()=>({resume:[...document.body.innerText.matchAll(/Yugal_Teotia_[A-Za-z0-9_]+\\.pdf/g)].map(m=>m[0])[0]||null,
          uploadControlPresent:!!document.querySelector('#attachCV')})""")
        profile['checked_at']=now()
    result={'job_id':220,'requisition_id':REQ,'listing_url':URL,'listing':listing,'profile':profile,
            'blocked_application_write_requests':blocked,'finished_at':now()}
    OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'evidence_path':str(OUT.relative_to(ROOT)),
                      'sha256':hashlib.sha256(OUT.read_bytes()).hexdigest(),
                      'listing':listing,'profile':profile,'blocked_writes':blocked},ensure_ascii=False))

if __name__=='__main__':main()
