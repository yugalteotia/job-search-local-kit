"""Inspect Applied Jobs navigation without disclosing other applications."""
from __future__ import annotations
import json
from naukri_direct_playwright import _chrome, _status

def main():
    with _chrome(headless=False) as context:
        page=context.pages[0] if context.pages else context.new_page()
        if not _status(page)['signed_in']:
            raise RuntimeError('Isolated Naukri session unavailable')
        page.goto('https://www.naukri.com/myapply/historypage',wait_until='domcontentloaded',timeout=30000)
        page.wait_for_timeout(2000)
        sweeps=[]
        stable=0
        previous=0
        for index in range(20):
            stats=page.evaluate("""() => {
              const box=document.querySelector('.jdTuplesContainer');
              return {height:document.body.scrollHeight,bodyLength:document.body.innerText.length,
                loadedRows:box?.children.length||0,scrollTop:box?.scrollTop||0,scrollHeight:box?.scrollHeight||0,
                exactIdVisible:document.body.innerText.includes('190826038395'),
                infosysVisible:/Infosys/i.test(document.body.innerText)};
            }""")
            sweeps.append(stats)
            if stats['exactIdVisible'] or stats['infosysVisible']:
                break
            if stats['bodyLength']==previous:
                stable+=1
            else:
                stable=0
            if stable>=3:
                break
            previous=stats['bodyLength']
            page.evaluate("() => {const e=document.querySelector('.jdTuplesContainer'); if(e)e.scrollTop=e.scrollHeight;}")
            page.wait_for_timeout(1000)
        result=page.evaluate("""() => {
          const text=document.body.innerText;
          const controls=[...document.querySelectorAll('button,a,[role=tab],[role=button],input[type=radio]')]
            .filter(e=>e.getClientRects().length).map(e=>({tag:e.tagName,role:e.getAttribute('role'),
              text:e.innerText?.trim().slice(0,65)||'',aria:e.getAttribute('aria-label'),
              title:e.getAttribute('title'),className:typeof e.className==='string'?e.className.slice(0,100):''}));
          return {path:location.pathname,headings:[...document.querySelectorAll('h1,h2,h3')].map(e=>e.innerText.trim()).slice(0,12),
            historyLabels:text.split('\\n').map(x=>x.trim()).filter(x=>/^(application history|applied jobs|all applications|naukri|other|all|show more|load more|next|previous|[0-9]+ applications?)$/i.test(x)).slice(0,30),
            scrollContainers:[...document.querySelectorAll('*')].filter(e=>e.scrollHeight>e.clientHeight+100&&e.clientHeight>100)
              .map(e=>({tag:e.tagName,id:e.id,className:typeof e.className==='string'?e.className.slice(0,90):'',clientHeight:e.clientHeight,scrollHeight:e.scrollHeight,children:e.children.length,overflowY:getComputedStyle(e).overflowY})).slice(0,15),
            listChildren:[...(document.querySelector('.jdTuplesContainer')?.children||[])].map(e=>({tag:e.tagName,className:typeof e.className==='string'?e.className.slice(0,100):'',textLength:e.innerText?.length||0,hasSent:/Application sent/.test(e.innerText||''),hasInfosys:/Infosys/i.test(e.innerText||'')})),
            listParentChildren:[...(document.querySelector('.jdTuplesContainer')?.parentElement?.children||[])].map(e=>({tag:e.tagName,className:typeof e.className==='string'?e.className.slice(0,90):'',textLength:e.innerText?.length||0})).slice(0,15),
            loadMore:[...document.querySelectorAll('.loadMoreLnk')].map(e=>({tag:e.tagName,href:e.getAttribute('href'),aria:e.getAttribute('aria-label'),title:e.getAttribute('title'),html:e.outerHTML.slice(0,300)})),
            filterOptions:[...(document.querySelector('.filterContainer')?.children||[])].map(e=>({tag:e.tagName,className:typeof e.className==='string'?e.className.slice(0,80):'',text:e.innerText.trim().slice(0,80)})),
            filterItems:[...(document.querySelector('.apply-filters')?.querySelectorAll('li,button,label,[role=tab],div')||[])].filter(e=>/Applies on Naukri|Applies on External Site|Recruiter Actions/.test(e.innerText||'')&&e.innerText.length<65)
              .map(e=>({tag:e.tagName,className:typeof e.className==='string'?e.className.slice(0,100):'',text:e.innerText.trim().slice(0,60),ariaSelected:e.getAttribute('aria-selected'),childClasses:[...e.querySelectorAll('*')].map(x=>typeof x.className==='string'?x.className.slice(0,70):'').filter(Boolean).slice(0,8)})).slice(0,20),
            controls:controls.filter(x=>x.text&&(/^(all|applied|naukri|others|next|previous|[0-9]{1,2})/i.test(x.text)||/pagination|page|filter/i.test(x.className))).slice(0,70),
            pagination:[...document.querySelectorAll('[class*=pagination],[class*=Pagination]')].map(e=>({tag:e.tagName,text:e.innerText.trim().slice(0,200),className:typeof e.className==='string'?e.className.slice(0,100):''})).slice(0,15),
            sentToday:(text.match(/Application sent today/g)||[]).length,
            infosysVisible:/Infosys/i.test(text), exactIdVisible:text.includes('190826038395')};
        }""")
        result['scroll_sweeps']=sweeps
        print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
