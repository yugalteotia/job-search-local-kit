"""Inspect the first Infosys screening answer without permitting a submission."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from naukri_direct_playwright import _chrome, _job_for_readonly_inspection, _status

JOB_ID = 220


def main() -> None:
    job = _job_for_readonly_inspection(JOB_ID)
    allowed_show = False
    allowed_answer = False
    first_node_nonleaf = False
    blocked = []
    with _chrome(headless=False) as context:
        page = context.pages[0] if context.pages else context.new_page()
        if not _status(page)["signed_in"]:
            raise RuntimeError("Isolated Naukri profile is not signed in")
        page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        if page.locator("#apply-button").count() == 0 or "Applied" in page.locator("#apply-button").first.inner_text():
            raise RuntimeError("Exact job no longer has an unapplied Apply action")

        def inspect_show_response(response):
            nonlocal first_node_nonleaf
            if urlsplit(response.url).path != "/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply":
                return
            try:
                payload=response.json()
                first_node_nonleaf=(response.status==200 and
                                    payload.get('chatbotResponse',{}).get('isLeafNode') is False)
            except Exception:
                first_node_nonleaf=False
        page.on('response',inspect_show_response)

        def route_request(route):
            nonlocal allowed_show, allowed_answer
            req = route.request
            if req.method in ("GET", "HEAD", "OPTIONS"):
                route.continue_(); return
            parts = urlsplit(req.url)
            item = {"method": req.method, "host": parts.hostname, "path": parts.path}
            if parts.path == "/cloudgateway-chatbot/chatbot-services/botapi/v5/respond":
                try:
                    payload=req.post_data_json
                    item["json_shape"]={k:(sorted(v.keys()) if isinstance(v,dict) else type(v).__name__) for k,v in payload.items()} if isinstance(payload,dict) else type(payload).__name__
                    answer=payload.get('input',{}).get('text') if isinstance(payload,dict) and isinstance(payload.get('input'),dict) else None
                    item['answer_is_confirmed_yes']=isinstance(answer,str) and answer.strip().casefold()=='yes'
                    item['answer_type']=type(answer).__name__
                    if isinstance(answer,list):
                        item['answer_list_length']=len(answer)
                        item['answer_item_shapes']=[sorted(x.keys()) if isinstance(x,dict) else type(x).__name__ for x in answer]
                        item['answer_list_is_exact_yes']=len(answer)==1 and isinstance(answer[0],str) and answer[0].strip().casefold()=='yes'
                    if (not allowed_answer and isinstance(payload,dict) and
                        isinstance(payload.get('input'),dict) and
                        (item['answer_is_confirmed_yes'] or item.get('answer_list_is_exact_yes'))
                        and first_node_nonleaf):
                        allowed_answer=True
                        route.continue_(); return
                except Exception:
                    item["json_shape"]="unavailable"
            if parts.path == "/cloudgateway-workflow/workflow-services/apply-workflow/v1/apply":
                try:
                    payload = req.post_data_json
                    item["json_keys"] = sorted(payload.keys()) if isinstance(payload, dict) else []
                    item["flowtype"] = payload.get("flowtype") if isinstance(payload, dict) else None
                    item["job_count"] = len(payload.get("strJobsarr", [])) if isinstance(payload, dict) else None
                except Exception:
                    pass
                if item.get("flowtype") == "show" and item.get("job_count") == 1 and not allowed_show:
                    allowed_show = True
                    route.continue_(); return
            blocked.append(item)
            route.abort()

        context.route("**/*", route_request)
        page.locator("#apply-button").first.click(timeout=10000)
        try:
            page.locator(".chatbot_Drawer #Yes").wait_for(timeout=10000)
        except Exception:
            print(json.dumps({"job_id":JOB_ID,"stopped":"first answer control absent",
                              "allowed_show":allowed_show,"blocked_requests":[x for x in blocked if x.get('host') and x['host'].endswith('naukri.com')],
                              "drawer_count":page.locator('.chatbot_Drawer').count(),
                              "visible_prompts":page.locator('.chatbot_Drawer .botMsg').all_inner_texts(),
                              "apply_text":page.locator('#apply-button').first.inner_text()},ensure_ascii=False))
            return
        prompt = page.locator(".chatbot_Drawer .botMsg").all_inner_texts()
        if not any("available for a interview?(Virtual)" in p for p in prompt):
            raise RuntimeError("Unexpected first screening question")
        upload_before=page.evaluate("""() => [...document.querySelectorAll('.chatbot_Drawer input[type=file]')].map(e=>({id:e.id,name:e.name,accept:e.accept,visible:!!e.getClientRects().length,parentText:e.parentElement?.innerText?.trim().slice(0,160)}))""")
        yes_label=page.locator('.chatbot_Drawer label[for="Yes"]')
        if yes_label.count()!=1:
            raise RuntimeError("Visible Yes label is missing or ambiguous")
        yes_label.click(timeout=10000)
        selected = page.locator(".chatbot_Drawer #Yes").is_checked()
        page.locator(".chatbot_Drawer .sendMsgbtn_container").click(timeout=10000)
        page.wait_for_timeout(2000)
        after = page.evaluate("""() => ({
          prompts:[...document.querySelectorAll('.chatbot_Drawer .botMsg')].map(e=>e.innerText.trim()),
          radios:[...document.querySelectorAll('.chatbot_Drawer input[type=radio]')].map(e=>({id:e.id,checked:e.checked})),
          fileInputs:document.querySelectorAll('.chatbot_Drawer input[type=file]').length,
          uploadControls:[...document.querySelectorAll('.chatbot_Drawer input[type=file]')].map(e=>({id:e.id,name:e.name,accept:e.accept,visible:!!e.getClientRects().length,parentText:e.parentElement?.innerText?.trim().slice(0,160)})),
          resumeNames:[...(document.querySelector('.chatbot_Drawer')?.innerText||'').matchAll(/Yugal_Teotia_[A-Za-z0-9_]+\\.pdf/g)].map(m=>m[0]),
          applied:/Applied/.test(document.querySelector('#apply-button')?.innerText||'')
        })""")
        print(json.dumps({"job_id": JOB_ID, "upload_before_answer":upload_before,"first_answer_selected": "Yes" if selected else None,
                          "show_request_allowed": allowed_show,"first_node_nonleaf":first_node_nonleaf,"yes_answer_request_allowed":allowed_answer,
                          "blocked_requests": [x for x in blocked if x["host"] and x["host"].endswith("naukri.com")],
                          "after": after, "final_submission_permitted": False}, ensure_ascii=False))

if __name__ == "__main__":
    main()
