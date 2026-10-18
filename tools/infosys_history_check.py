"""Read-only exact requisition check in isolated Naukri application history."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from naukri_direct_playwright import _chrome, _status

REQ = "190826038395"
HISTORY = "https://www.naukri.com/myapply/historypage"


def main():
    with _chrome(headless=False) as context:
        page=context.pages[0] if context.pages else context.new_page()
        if not _status(page)["signed_in"]:
            raise RuntimeError("Isolated Naukri session is not signed in")
        page.goto(HISTORY,wait_until="domcontentloaded",timeout=30000)
        page.wait_for_timeout(2500)
        result=page.evaluate("""req => {
          const links=[...document.querySelectorAll('a[href]')].filter(a=>a.href.includes(req));
          const text=document.body.innerText;
          const company=text.toLowerCase().includes('infosys');
          return {path:location.pathname, title:document.title,
                  exactLinks:links.map(a=>({path:new URL(a.href).pathname,text:a.innerText.trim().slice(0,140)})),
                  exactIdVisible:text.includes(req), companyVisible:company,
                  roleVisible:/Java Backend Developer/i.test(text),
                  appliedLanguage:/Application sent|Applied on|Application submitted/i.test(text),
                  nxtwaveVisible:/Nxtwave/i.test(text),
                  applicationSentTodayCount:(text.match(/Application sent today/g)||[]).length,
                  bodyLength:text.length,
                  filterLabels:[...document.querySelectorAll('button,[role=tab],input[type=radio]')].map(e=>e.innerText?.trim()).filter(x=>x&&/naukri|all|applied/i.test(x)).slice(0,12),
                  noApplications:/no applications|no jobs applied/i.test(text)};
        }""",REQ)
        print(json.dumps(result,ensure_ascii=False))

if __name__=="__main__":
    main()
