# Direct Naukri Playwright prototype

This is a **no-submit** browser experiment. It uses installed Google Chrome and
an isolated persistent profile at `~/.local/share/job-search-private/naukri-direct-playwright/user-data`.
The directory is owner-only (0700), created with a restrictive umask, locked so
two runs cannot share it, and never copied from the personal Chrome profile.
No browser or cookie archive is stored in this repository. The `.gitignore`
also excludes common auth-state filenames as a defense against accidental copies.

Run from this project directory:

```bash
PYTHONPATH=.local-deps python3 tools/naukri_direct_playwright.py launch-check
PYTHONPATH=.local-deps python3 tools/naukri_direct_playwright.py local-upload-test master-resume/Yugal_Teotia_CV.pdf
PYTHONPATH=.local-deps python3 tools/naukri_direct_playwright.py login
PYTHONPATH=.local-deps python3 tools/naukri_direct_playwright.py session-check --visible
PYTHONPATH=.local-deps python3 tools/naukri_direct_playwright.py profile-resume-status
PYTHONPATH=.local-deps python3 tools/naukri_direct_playwright.py profile-upload-same
PYTHONPATH=.local-deps python3 tools/naukri_direct_playwright.py inspect-job JOB_ID
```

`login` must run in your own interactive terminal. Sign in only inside the
new Chrome window; the script never prompts for a password or OTP. A later
`session-check` relaunches the same isolated profile to test reuse. `inspect-job`
only opens an unapplied Naukri listing selected from SQLite and checks the
visible page; it never clicks Apply. The local upload test uses a dummy HTML
form and the actual PDF, then checks the Naukri-style radio/Save enablement
without clicking Save or sending a network request.

Naukri's signed-in profile control did not appear in a headless relaunch, so
real Naukri checks use visible Chrome. `profile-upload-same` is an optional
live upload check: it runs only when the generic PDF is already shown as the
profile default, uploads that same file, observes the upload response status,
then reloads to verify the generic filename remains visible. It does not open
an application form. Naukri clears the file input after handling the upload,
so checking `files[]` after the event would report a false failure.

On 2026-09-29, visible session reuse succeeded; read-only inspection of SQLite
job 209 opened the exact Naukri listing. The local dummy PDF and chat-radio
test passed. Live same-file profile upload returned HTTP 200 and the generic
filename was still visible after reload. Job 209 remains Medium fit, so its
Apply control was not clicked; live application questionnaire and job-specific
attachment remain unverified.

The existing workflow's fit, history-dedupe, unique-attempt, exact resume/form
binding, proof-before-Applied, and read-only schedule remain unchanged. The
prototype cannot mark a listing Applied and has no Submit action. A live
Naukri questionnaire or employer upload is unverified until tested on an
eligible, unapplied requisition without triggering a one-click submission.
