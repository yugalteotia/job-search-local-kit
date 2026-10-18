"""Read-only audit of one locked Infosys Naukri application outcome.

No Apply, chatbot answer, upload, or submission control is clicked. The
request guard also aborts those endpoints if a page tries to invoke them.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from naukri_direct_playwright import ROOT, PROFILE_URL, _chrome, _status
from job_db import connect

JOB_ID = 220
REQ = "190826038395"
HISTORY_URL = "https://www.naukri.com/myapply/historypage"
OUT = ROOT / "jobs" / "naukri-190826038395" / "readonly-outcome-audit.json"


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def main() -> None:
    db = connect()
    try:
        job = db.execute("SELECT id,requisition_id,canonical_url,state,slug FROM jobs WHERE id=?", (JOB_ID,)).fetchone()
        if not job or job["requisition_id"] != REQ or job["slug"] != "naukri-190826038395":
            raise RuntimeError("Exact SQLite job binding failed")
        if job["state"] != "NeedsReview":
            raise RuntimeError("The locked job state changed; stop audit")
        url = job["canonical_url"]
        if urlsplit(url).hostname != "www.naukri.com" or not url.endswith(REQ):
            raise RuntimeError("Exact Naukri listing URL binding failed")
        attempts = db.execute("SELECT count(*) FROM submission_attempts WHERE job_id=?", (JOB_ID,)).fetchone()[0]
    finally:
        db.close()

    blocked_requests = []
    with _chrome(headless=False) as context:
        page = context.pages[0] if context.pages else context.new_page()

        def prevent_application_write(route):
            req = route.request
            path = urlsplit(req.url).path.lower()
            if req.method not in ("GET", "HEAD", "OPTIONS") and any(x in path for x in
                ("/apply-workflow/", "/botapi/", "/upload", "/resume", "/submit")):
                blocked_requests.append({"method": req.method, "path": path})
                route.abort()
            else:
                route.continue_()
        context.route("**/*", prevent_application_write)

        if not _status(page)["signed_in"]:
            raise RuntimeError("Isolated Chrome profile is not signed in")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1800)
        listing = page.evaluate("""req => {
          const buttons=[...document.querySelectorAll('button')].filter(e=>e.getClientRects().length);
          const text=document.body.innerText;
          const names=[...text.matchAll(/Yugal_Teotia_[A-Za-z0-9_]+\\.pdf/g)].map(m=>m[0]);
          return {path:location.pathname,title:document.title,exactIdInPath:location.pathname.endsWith(req),
            applyButtonCount:buttons.filter(e=>e.id==='apply-button'&&e.innerText.trim()==='Apply').length,
            appliedButtonCount:buttons.filter(e=>e.id==='apply-button'&&/Applied/i.test(e.innerText)).length,
            appliedStatusVisible:/(^|\\n)Applied(\\n|$)/.test(text),
            pendingLanguageVisible:/Continue application|Pending application|Complete application/i.test(text),
            chatDrawerVisible:!![...document.querySelectorAll('.chatbot_Drawer')].find(e=>e.getClientRects().length),
            chatPrompts:[...document.querySelectorAll('.chatbot_Drawer .botMsg')].filter(e=>e.getClientRects().length).map(e=>e.innerText.trim()),
            fileInputCount:[...document.querySelectorAll('.chatbot_Drawer input[type=file]')].filter(e=>e.getClientRects().length).length,
            resumeNames:names};
        }""", REQ)
        listing["checked_at"] = timestamp()
        listing["url"] = url

        page.goto(HISTORY_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2400)
        history = page.evaluate("""req => {
          const text=document.body.innerText;
          const links=[...document.querySelectorAll('a[href]')];
          const matching=links.filter(a=>a.href.includes(req));
          const nav=[...document.querySelectorAll('button,a,[role=tab]')].filter(e=>e.getClientRects().length)
            .map(e=>({tag:e.tagName,text:e.innerText.trim().slice(0,50),path:e.href?new URL(e.href).pathname:null}))
            .filter(x=>/^(next|previous|[0-9]{1,3}|all|naukri|applied)/i.test(x.text)).slice(0,35);
          return {path:location.pathname,title:document.title,bodyLength:text.length,
            exactIdVisible:text.includes(req),exactLinks:matching.map(a=>({path:new URL(a.href).pathname,text:a.innerText.trim().slice(0,120)})),
            infosysVisible:/Infosys/i.test(text),exactRoleVisible:/Java Backend Developer/i.test(text),
            nxtwaveVisible:/Nxtwave/i.test(text),recentSentRows:(text.match(/Application sent today/g)||[]).length,
            navigationControls:nav};
        }""", REQ)
        history["checked_at"] = timestamp()
        history["url"] = HISTORY_URL

        page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1400)
        profile = page.evaluate("""() => {
          const text=document.body.innerText;
          const names=[...text.matchAll(/Yugal_Teotia_[A-Za-z0-9_]+\\.pdf/g)].map(m=>m[0]);
          return {path:location.pathname,defaultResumeFilename:names[0]||null,
            resumeUploadControlPresent:!!document.querySelector('#attachCV')};
        }""")
        profile["checked_at"] = timestamp()

    result = {"audit_started_at": listing["checked_at"], "audit_finished_at": timestamp(),
              "job_id": JOB_ID, "requisition_id": REQ, "sqlite_state_before": "NeedsReview",
              "submission_attempt_rows": attempts, "listing": listing, "history": history,
              "profile": profile, "application_write_requests_blocked": blocked_requests,
              "controls_clicked": [], "application_mutation_attempted": False}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"evidence_path": str(OUT.relative_to(ROOT)),
                      "sha256": hashlib.sha256(OUT.read_bytes()).hexdigest(),
                      "listing": listing, "history": history, "profile": profile,
                      "application_write_requests_blocked": blocked_requests}, ensure_ascii=False))


if __name__ == "__main__":
    main()
