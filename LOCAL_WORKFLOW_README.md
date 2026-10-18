# Job Search — Local Workflow Kit

> Historical export notes below describe the initial kit. For the current SQLite-backed, read-only staged migration and commands, see [WORKFLOW_README.md](WORKFLOW_README.md) and [ARCHITECTURE_V2.md](ARCHITECTURE_V2.md). `tracker.xlsx` is now a generated view, not the source of truth.

Exported 2026-09-27 IST. This kit lets you run the job search on your own
computer, in your own signed-in browser. Nothing here depends on the cloud
workspace.

## State at export time
- Cloud schedules **PAUSED** (10-min sweep and morning digest both disabled).
- **Zero applications were ever submitted** from the cloud workflow.
- Earliest joining date: **1 October 2026** (confirmed; last working day at
  IDeaS is 30 Sep 2026).
- No portal logins were ever saved; no accounts were created.

## What's in this kit
- `tracker.xlsx` — master tracker (sheets: `Tracker`, `Guide`). Direct
  posting URLs are plain text. Statuses used: Shortlisted / Applied /
  Excluded / Submission unconfirmed.
- `jobs/<company>-<role>/` — one folder per opening:
  - `JD.md` — saved job description with capture date (posting date is
    recorded ONLY when the listing itself states it).
  - `record.md` — fit verdict with evidence, key gaps, next step.
  - staged artifacts where prepared (e.g. tailored 1-page resume PDF,
    `resume_spec.json`).
- `SEARCH_PROFILE.md` — targets, fit criteria (High/Medium/Low),
  dedupe and exclusion rules, truthfulness rules.
- `APPLICATION_PROFILE.md` — your confirmed answers for application forms:
  contact details, experience, reason for leaving, career-gap explanation,
  salary figures (enter only when mandatory), joining date.
- `tools/update_tracker.py` — add/update tracker rows from JSON
  (needs Python 3 + `openpyxl`).
- `tools/build_tracker.py` — rebuild the spreadsheet from scratch.
- `tools/render_resume.py` — build a tailored 1-page PDF from a
  `resume_spec.json` (needs `reportlab`).
- `master-resume/` — your generic master CV (.tex + .pdf). Always tailor
  FROM this master, never from a prior company-tailored resume.

## Your application rules (summary)
1. Apply only to verified **High-fit** backend or development-focused
   full-stack roles in India; the original listing must be live; never
   apply twice to the same requisition.
2. A prepared/filled form is NOT an application. Count it only after a
   confirmation page, confirmation email, or application ID is captured.
   If uncertain, mark "Submission unconfirmed" and investigate — never
   retry blindly.
3. Never guess mandatory answers and never misrepresent: project/training-
   only skills (Kotlin/Compose, MySQL/JPA/Flyway/OpenAPI, basic React,
   AI-assisted UNITY work) must be labeled as such. Do not claim
   production cloud ownership, Kubernetes, Kafka, Redis/Elasticsearch at
   scale, Python/Go, or system-design ownership.
4. DOB and full address live ONLY inside application forms — never in the
   tracker, records, or chat.
5. Salary: enter truthful figures (current 9 LPA, expected ~15 LPA) only
   when the field is mandatory. Never reject a strong role just because
   salary is undisclosed.
6. Reason for leaving IDeaS (exact wording): "My role at IDeaS ended, and
   I'm seeking a backend-focused software engineering position where I can
   build services and APIs." Never claim a shift toward QA/testing.
7. Gap Jun 2023–Feb 2024 (only if a form explicitly asks): "Preparing for
   C-DAC's C-CAT entrance examination and self-learning/upskilling in new
   technologies."

## Suggested local loop
1. Search in your signed-in browser: Naukri, Foundit, Shine, Cutshort,
   LinkedIn, company career/ATS boards.
2. Open the ORIGINAL listing; verify it is live. Save it as
   `jobs/<company>-<role>/JD.md` with today's date.
3. Dedupe against `tracker.xlsx` (Company + Job ID + Role).
4. Rank High/Medium/Low with an evidence-based reason; exclude expired,
   scam, or paid-placement postings and roles clearly beyond scope.
5. For High-fit: write a `resume_spec.json` from the generic master and
   render the tailored PDF with `tools/render_resume.py`.
6. Submit in your browser. Capture confirmation evidence (screenshot of
   the confirmation page, confirmation email, or application ID) and
   record it in the tracker's Submission date / Confirmation evidence
   columns plus the record's audit trail.

## Staged and ready for manual submission
1. **UnitedHealth Group (Optum) — Software Engineer, Backend Developer**
   (Noida, High fit)
   - https://www.shine.com/jobs/software-engineer-backend-developer/unitedhealth-group/19568565
   - Tailored resume:
     jobs/unitedhealth-group-software-engineer-backend-developer/UnitedHealthGroup_BackendDeveloper_Resume.pdf
2. **Optum — Software Engineer (Java/Angular)** (Noida, Medium/stretch —
   wants 3+ yrs plus Kafka/Python/cloud; your call)
   - https://builtin.com/job/software-engineer-java-angular/9947706

## Lead worth retrying
- **Worldpay — Java Developer (Spring Boot, REST API), 2–4 yrs,
  Pune/Bangalore** — looked strong but the page failed to load during
  verification. Not verified, not in tracker.
  - https://www.shine.com/jobs/java-developer-spring-boot-rest-api-2-4-yrs-indore-pune-bangalore-bengaluru/worldpay/19220798
