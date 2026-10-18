"""One-job, no-submit inspection of the isolated Naukri application flow."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from naukri_direct_playwright import ROOT, _chrome, _job_for_readonly_inspection, _status

JOB_ID = 220


def main() -> None:
    job = _job_for_readonly_inspection(JOB_ID)
    assert job["url"].endswith("190826038395")
    with _chrome(headless=False) as context:
        page = context.pages[0] if context.pages else context.new_page()
        if not _status(page)["signed_in"]:
            raise RuntimeError("Isolated profile is not signed in")
        page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        before = page.evaluate("""() => {
          const b=document.querySelector('#apply-button');
          return {path:location.pathname,button:b&&{text:b.innerText.trim(),tag:b.tagName,type:b.getAttribute('type'),href:b.getAttribute('href')},
                  applied:/(^|\\n)Applied(\\n|$)/.test(document.body.innerText),
                  resumeMentions:[...document.body.innerText.matchAll(/Yugal_Teotia_[A-Za-z0-9_]+\\.pdf/g)].map(m=>m[0])};
        }""")
        if before["path"] != urlsplit(job["url"]).path or before["applied"] or not before["button"]:
            print(json.dumps({"job_id":JOB_ID,"before":before,"stopped":"exact listing, Apply, or unapplied gate failed"}))
            return
        blocked=[]
        allowed_show=[]
        show_response=[]
        def inspect_show_response(response):
            if urlsplit(response.url).path != "/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply":
                return
            item={"status":response.status}
            try:
                body=response.json()
                item["shape"]={k:(sorted(v.keys()) if isinstance(v,dict) else (f"list:{len(v)}" if isinstance(v,list) else type(v).__name__)) for k,v in body.items()} if isinstance(body,dict) else type(body).__name__
                if isinstance(body,dict) and isinstance(body.get('chatbotResponse'),dict):
                    node=body['chatbotResponse']
                    item['flow_flags']={k:node.get(k) for k in ('isApply','isLeafNode','dataCommitted','progress','actionType','currentNodeName')}
            except Exception:
                item["shape"]="non-json"
            show_response.append(item)
        page.on("response",inspect_show_response)
        def block_mutations(route):
            req=route.request
            if req.method not in ("GET","HEAD","OPTIONS"):
                parts=urlsplit(req.url)
                item={"method":req.method,"host":parts.hostname,"path":parts.path}
                if parts.path == "/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply":
                    try:
                        payload=req.post_data_json
                        item["json_shape"]={k:(sorted(v.keys()) if isinstance(v,dict) else type(v).__name__) for k,v in payload.items()} if isinstance(payload,dict) else type(payload).__name__
                        if isinstance(payload,dict):
                            item["workflow_flags"]={k:payload.get(k) for k in ("flowtype","chatBotSDK","applyTypeId","applySrc")}
                            item["job_count"]=len(payload.get("strJobsarr",[])) if isinstance(payload.get("strJobsarr"),list) else None
                    except Exception:
                        item["json_shape"]="unavailable"
                if (parts.path == "/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply"
                    and item.get("workflow_flags",{}).get("flowtype") == "show"
                    and item.get("job_count") == 1 and not allowed_show):
                    allowed_show.append(item)
                    route.continue_()
                else:
                    blocked.append(item)
                    route.abort()
            else:
                route.continue_()
        context.route("**/*", block_mutations)
        page.locator("#apply-button").first.click(timeout=10000)
        page.wait_for_timeout(2500)
        after=page.evaluate("""() => ({path:location.pathname, title:document.title,
          drawer:!!document.querySelector('.chatbot_Drawer'),
          dialogCount:document.querySelectorAll('[role=dialog]').length,
          inputs:[...document.querySelectorAll('input,select,textarea')].filter(e=>e.getClientRects().length).map(e=>({tag:e.tagName,type:e.type,name:e.name,id:e.id,placeholder:e.placeholder,labels:[...(e.labels||[])].map(x=>x.innerText),value:e.type==='file'?null:(e.type==='radio'||e.type==='checkbox'?e.checked:null)})),
          buttons:[...document.querySelectorAll('button')].filter(e=>e.getClientRects().length).map(e=>e.innerText.trim()).filter(Boolean).slice(-25),
          resumeMentions:[...document.body.innerText.matchAll(/Yugal_Teotia_[A-Za-z0-9_]+\\.pdf/g)].map(m=>m[0]),
          chatPrompts:[...document.querySelectorAll('.botMsg,[class*=botMsg],[class*=question]')].filter(e=>e.getClientRects().length).map(e=>e.innerText.trim()).filter(Boolean).slice(-12),
          drawerControls:[...document.querySelectorAll('.chatbot_Drawer button,.chatbot_Drawer input,.chatbot_Drawer [role=button],.chatbot_Drawer [class*=sendMsg]')].filter(e=>e.getClientRects().length).map(e=>({tag:e.tagName,type:e.getAttribute('type'),id:e.id,className:typeof e.className==='string'?e.className.slice(0,120):'',text:e.innerText?.trim().slice(0,80)||'',disabled:e.disabled||false})).slice(-30),
          resumeRelatedText:(document.querySelector('.chatbot_Drawer')?.innerText||'').split('\\n').map(x=>x.trim()).filter(x=>/resume|cv|upload|attach|file/i.test(x)).slice(0,15)
        })""")
        result={"job_id":JOB_ID,"before":before,"allowed_show":allowed_show,"show_response":show_response,"blocked_mutations":blocked,"after":after}
        artifact=ROOT / "jobs" / "naukri-190826038395" / "form_inspection.json"
        artifact.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
        print(json.dumps(result,ensure_ascii=False))

if __name__ == "__main__":
    main()
