"""No-submit direct-Playwright prototype with an isolated Chrome profile.

This deliberately has no application submit method. A persistent profile outside
the repository stores Naukri authentication after a manual login. The profile
is never copied from Chrome's personal User Data directory.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".local-deps"))
sys.path.insert(0, str(ROOT / "tools"))

from job_db import connect  # noqa: E402

PRIVATE_ROOT = Path.home() / ".local" / "share" / "job-search-private" / "naukri-direct-playwright"
PROFILE = PRIVATE_ROOT / "user-data"
LOGIN_URL = "https://www.naukri.com/nlogin/login"
PROFILE_URL = "https://www.naukri.com/mnjuser/profile"


def _private_profile() -> Path:
    """Make the isolated browser profile owner-only; reject symlink redirection."""
    root = PRIVATE_ROOT.expanduser()
    if root.is_symlink() or PROFILE.is_symlink():
        raise RuntimeError("Private browser path cannot be a symlink")
    if root.resolve().is_relative_to(ROOT.resolve()):
        raise RuntimeError("Browser authentication cannot be stored in the project")
    personal = (Path.home() / ".config" / "google-chrome").resolve()
    if root.resolve().is_relative_to(personal):
        raise RuntimeError("Refusing the personal Chrome profile")
    old_umask = os.umask(0o077)
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        PROFILE.mkdir(mode=0o700, exist_ok=True)
        root.chmod(0o700)
        PROFILE.chmod(0o700)
    finally:
        os.umask(old_umask)
    for path in (root, PROFILE):
        info = path.lstat()
        if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise RuntimeError("Private browser directory must be owned by you with mode 0700")
    return PROFILE


@contextlib.contextmanager
def _locked_profile():
    profile = _private_profile()
    lock_path = PRIVATE_ROOT / "profile.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield profile
    except BlockingIOError as exc:
        raise RuntimeError("Isolated Naukri browser is already in use") from exc
    finally:
        os.close(fd)


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Install project requirements; no browser download is needed") from exc
    return sync_playwright


@contextlib.contextmanager
def _chrome(*, headless: bool, read_only: bool = False):
    with _locked_profile() as profile:
        old_umask = os.umask(0o077)
        try:
            with _playwright()() as pw:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile), channel="chrome", headless=headless,
                    accept_downloads=False, timeout=30_000,
                    service_workers='block' if read_only else 'allow',
                )
                try:
                    yield context
                finally:
                    context.close()
        finally:
            os.umask(old_umask)


def _status(page) -> dict:
    page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=30_000)
    try:page.wait_for_selector('#attachCV',state='attached',timeout=12000)
    except Exception:pass
    path = urlsplit(page.url).path
    signed_in = path.startswith("/mnjuser/profile") and page.locator("#attachCV").count() == 1
    return {"signed_in": signed_in, "profile_page": path.startswith("/mnjuser/profile")}


def launch_check(headless: bool) -> dict:
    with _chrome(headless=headless) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("about:blank")
        return {"chrome_opened": True, "browser_version": context.browser.version,
                "mode": "headless" if headless else "visible", "isolated_profile": True}


def login() -> dict:
    if not sys.stdin.isatty():
        raise RuntimeError("Run login in your own terminal so you can type credentials into Chrome")
    with _chrome(headless=False) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        print("Sign in inside the separate Chrome window; complete any OTP/CAPTCHA there.")
        input("When Naukri shows your signed-in account, press Enter here to verify: ")
        return {"manual_login_complete": _status(page)["signed_in"],
                "auth_location": "owner-only directory outside repository"}


def session_check(*, headless: bool) -> dict:
    with _chrome(headless=headless) as context:
        page = context.pages[0] if context.pages else context.new_page()
        return {**_status(page), "reused_isolated_profile": True,
                "mode": "headless" if headless else "visible"}


def _job_for_readonly_inspection(job_id: int):
    db = connect()
    try:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise RuntimeError("Unknown SQLite job ID")
        if not str(row["source"] or "").startswith("naukri"):
            raise RuntimeError("Only a Naukri-sourced job can be inspected")
        if row["state"] in ("Applied", "Rejected", "Closed", "Expired", "Withdrawn", "SubmissionUnconfirmed"):
            raise RuntimeError("Existing or uncertain application: do not reopen it")
        if db.execute("SELECT 1 FROM submission_attempts WHERE job_id=?", (job_id,)).fetchone():
            raise RuntimeError("Existing attempt: do not reopen it")
        if db.execute("SELECT 1 FROM history_keys WHERE employer_key=? AND requisition_key=?",
                      (row["employer_key"], row["requisition_key"])).fetchone():
            raise RuntimeError("Historical requisition: do not reopen it")
        url = row["canonical_url"] or ""
        host = urlsplit(url).hostname
        if host not in ("www.naukri.com", "naukri.com") or not url.startswith("https://"):
            raise RuntimeError("Job URL is not an exact HTTPS Naukri listing")
        return {"job_id": job_id, "url": url, "fit": row["fit"], "state": row["state"]}
    finally:
        db.close()


def inspect_job(job_id: int) -> dict:
    job = _job_for_readonly_inspection(job_id)
    # Naukri has returned a profile page without its signed-in controls in
    # headless Chrome. Keep real-site checks in the same visible mode as login.
    with _chrome(headless=False) as context:
        page = context.pages[0] if context.pages else context.new_page()
        if not _status(page)["signed_in"]:
            return {"job_id": job_id, "signed_in": False, "detail_checked": False}
        page.goto(job["url"], wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1500)
        detail = page.evaluate("""() => ({
          host: location.hostname, path: location.pathname,
          title: document.title,
          apply_visible: [...document.querySelectorAll('#apply-button')].some(e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)),
          applied_visible: /(^|\\n)Applied(\\n|$)/.test(document.body.innerText),
          drawer_visible: !!document.querySelector('.chatbot_Drawer'),
          file_inputs: document.querySelectorAll('input[type=file]').length
        })""")
        expected_path = urlsplit(job["url"]).path
        return {"job_id": job_id, "signed_in": True, "detail_checked": True,
                "exact_job": detail["host"] in ("www.naukri.com", "naukri.com") and detail["path"] == expected_path,
                "fit": job["fit"], "state": job["state"], "apply_visible": detail["apply_visible"],
                "already_applied_visible": detail["applied_visible"], "drawer_visible": detail["drawer_visible"],
                "job_file_inputs": detail["file_inputs"], "form_opened": False,
                "submission_attempted": False}


def profile_resume_status() -> dict:
    """Read only: inspect the isolated account's profile resume filename."""
    with _chrome(headless=False) as context:
        page = context.pages[0] if context.pages else context.new_page()
        if not _status(page)["signed_in"]:
            return {"signed_in": False, "resume_filename": None, "upload_input_present": False}
        data = page.evaluate("""() => {
          const input = document.querySelector('#attachCV');
          const text = document.body.innerText;
          const matches = [...text.matchAll(/Yugal_Teotia_[A-Za-z0-9_]+\\.pdf/g)].map(m => m[0]);
          return {filename: matches[0] || null,
                  upload_input_present: !!input && input.type === 'file'};
        }""")
        return {"signed_in": True, **data}


def profile_upload_same() -> dict:
    """Exercise real Naukri upload only when the same generic PDF is current."""
    pdf = ROOT / "master-resume" / "Yugal_Teotia_CV.pdf"
    if not pdf.is_file():
        raise RuntimeError("Generic resume PDF is missing")
    with _chrome(headless=False) as context:
        page = context.pages[0] if context.pages else context.new_page()
        if not _status(page)["signed_in"]:
            raise RuntimeError("Isolated Naukri profile is not signed in")
        displayed = re.findall(r"Yugal_Teotia_[A-Za-z0-9_]+\.pdf", page.locator("body").inner_text())
        if not displayed or displayed[0] != pdf.name:
            raise RuntimeError("Generic resume is not the current profile default; upload stopped")
        upload = page.locator("#attachCV")
        if upload.count() != 1 or upload.get_attribute("type") != "file":
            raise RuntimeError("Naukri resume file input is unavailable")
        upload_responses: list[int] = []
        def record_upload(response):
            path = urlsplit(response.url).path.lower()
            if (response.request.method in ("POST", "PUT", "PATCH")
                    and any(word in path for word in ("resume", "upload", "cv"))
                    and (urlsplit(response.url).hostname or "").endswith("naukri.com")):
                upload_responses.append(response.status)
        page.on("response", record_upload)
        upload.set_input_files(str(pdf))
        # The site clears its file input immediately after handling a change,
        # so a later files[] inspection is not a reliable upload check.
        page.wait_for_timeout(5000)
        page.reload(wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1500)
        return {"signed_in": _status(page)["signed_in"],
                "upload_response_statuses": upload_responses,
                "profile_still_displays_generic_resume": pdf.name in page.locator("body").inner_text(),
                "application_form_opened": False, "application_submitted": False}


def local_upload_and_chat_test(pdf: Path) -> dict:
    if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
        raise RuntimeError("Choose an existing PDF")
    with _chrome(headless=True) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.set_content("""<!doctype html><title>Local no-submit Naukri fixture</title>
        <input type=file id=resume accept=application/pdf>
        <div class=chatbot_Drawer><div class=botMsg>Willing to relocate to Bengaluru?</div>
        <input type=radio id=yes name=relocate><label for=yes>Yes</label>
        <div id=sendMsg__fixture class='send disabled'><div class=sendMsg>Save</div></div></div>
        <script>document.querySelector('#yes').onchange=()=>document.querySelector('.send').classList.remove('disabled')</script>""")
        page.locator("#resume").set_input_files(str(pdf))
        attached = page.locator("#resume").evaluate("e => e.files.length === 1 && e.files[0].name")
        page.locator('label[for="yes"]').click()
        chat_ok = page.locator("#yes").is_checked() and "disabled" not in (page.locator(".send").get_attribute("class") or "")
        return {"chrome_opened": True, "upload_attached": attached == pdf.name,
                "attachment_filename": attached, "local_chat_option_registered": chat_ok,
                "chat_save_clicked": False, "network_submission_attempted": False,
                "fixture": "local HTML only"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("launch-check").add_argument("--visible", action="store_true")
    sub.add_parser("login")
    sub.add_parser("session-check").add_argument("--visible", action="store_true")
    sub.add_parser("profile-resume-status")
    sub.add_parser("profile-upload-same")
    sub.add_parser("inspect-job").add_argument("job_id", type=int)
    sub.add_parser("local-upload-test").add_argument("pdf", type=Path)
    args = parser.parse_args()
    if args.command == "launch-check": result = launch_check(headless=not args.visible)
    elif args.command == "login": result = login()
    elif args.command == "session-check": result = session_check(headless=not args.visible)
    elif args.command == "profile-resume-status": result = profile_resume_status()
    elif args.command == "profile-upload-same": result = profile_upload_same()
    elif args.command == "inspect-job": result = inspect_job(args.job_id)
    else: result = local_upload_and_chat_test(args.pdf.resolve())
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try: main()
    except Exception as exc:
        # Never print browser URLs, cookies, paths, or profile contents from
        # Playwright exceptions; they can contain private session material.
        print(json.dumps({"error_type": type(exc).__name__, "error": str(exc) if isinstance(exc, RuntimeError) else "Browser operation failed"}))
        raise SystemExit(1)
