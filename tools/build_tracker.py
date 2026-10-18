#!/usr/bin/env python3
"""Build (or rebuild) ~/workspace/job-search/tracker.xlsx — the primary shared tracker.
One row per requisition. Safe to re-run: it rewrites the Tracker sheet from ROWS below,
preserving nothing. For incremental updates use update_tracker.py.
"""
from local_paths import ROOT
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

PATH = str(ROOT / "tracker.xlsx")

COLUMNS = [
    "Company", "Role", "Job ID", "Direct link", "Source",
    "Location", "Work arrangement", "Posted date", "Verified date",
    "Fit", "Fit rationale", "Key gaps", "Salary info",
    "Resume version", "Status", "Submission date", "Confirmation evidence",
    "Referral status", "Responses", "Deadlines", "Next step",
]

# Pre-existing requisitions as of 2026-09-27.
ROWS = [
    {
        "Company": "Technorex Infoways",
        "Role": "Senior JAVA Spring Boot Backend API Developer",
        "Job ID": "19510106",
        "Direct link": "https://www.shine.com/jobs/senior-java-spring-boot-backend-api-developer-remote/technorex-infoways/19510106",
        "Source": "Shine.com",
        "Location": "Remote, All India",
        "Work arrangement": "Remote",
        "Posted date": "", "Verified date": "2026-09-27",
        "Fit": "Medium",
        "Fit rationale": "Strong Java/Spring Boot/REST/microservices/database overlap; downgraded: thin company profile, masked contact email, 'Senior' title inflated for 2-5yr band.",
        "Key gaps": "Company legitimacy unverified",
        "Salary info": "Undisclosed",
        "Resume version": "", "Status": "Shortlisted", "Submission date": "",
        "Confirmation evidence": "", "Referral status": "None",
        "Responses": "", "Deadlines": "",
        "Next step": "Verify company legitimacy before tailoring; Medium fit is below autonomous-submit bar (High only).",
    },
    {
        "Company": "Teamware Solutions",
        "Role": "Java Spring Boot Developer",
        "Job ID": "35483913",
        "Direct link": "https://www.foundit.in/job/java-spring-boot-developer-teamware-solutions-hyderabad-secunderabad-telangana-35483913",
        "Source": "Foundit",
        "Location": "Hyderabad / Secunderabad",
        "Work arrangement": "Unknown (agency posting)",
        "Posted date": "", "Verified date": "2026-09-27",
        "Fit": "Medium",
        "Fit rationale": "Strong Java/Spring/REST/testing/build-tools overlap; production RDBMS/JPA depth not established.",
        "Key gaps": "Client, work mode, employment type, salary unknown (agency posting)",
        "Salary info": "Undisclosed",
        "Resume version": "", "Status": "Shortlisted", "Submission date": "",
        "Confirmation evidence": "", "Referral status": "None",
        "Responses": "", "Deadlines": "",
        "Next step": "Verify client/mode/type/salary before any application; Medium fit is below autonomous-submit bar.",
    },
    {
        "Company": "Hevo Data",
        "Role": "SDE I Backend",
        "Job ID": "6cbbe304-e065-4711-bf3e-756795d2bc2a",
        "Direct link": "https://jobs.lever.co/hevodata/6cbbe304-e065-4711-bf3e-756795d2bc2a",
        "Source": "Lever (company ATS)",
        "Location": "Bengaluru",
        "Work arrangement": "On-site, full-time",
        "Posted date": "", "Verified date": "2026-09-27",
        "Fit": "Medium (junior band)",
        "Fit rationale": "Genuine product-company backend work: Java/REST/data pipelines; but stated persona 0-1 yrs is junior to his profile.",
        "Key gaps": "Band junior to profile; compensation likely below target",
        "Salary info": "Undisclosed (likely below target)",
        "Resume version": "", "Status": "Shortlisted", "Submission date": "",
        "Confirmation evidence": "", "Referral status": "None",
        "Responses": "", "Deadlines": "",
        "Next step": "Pursue only if Yugal accepts the junior band — needs his call.",
    },
    {
        "Company": "Technum Opus (Onified.ai)",
        "Role": "SDE II",
        "Job ID": "",
        "Direct link": "",
        "Source": "Cutshort",
        "Location": "", "Work arrangement": "",
        "Posted date": "", "Verified date": "2026-09-27",
        "Fit": "Excluded",
        "Fit rationale": "Listing marked closed.",
        "Key gaps": "", "Salary info": "",
        "Resume version": "", "Status": "Excluded — closed", "Submission date": "",
        "Confirmation evidence": "", "Referral status": "None",
        "Responses": "", "Deadlines": "",
        "Next step": "Logged for dedupe only.",
    },
    {
        "Company": "Amazon (via Cutshort)",
        "Role": "SDE 2",
        "Job ID": "",
        "Direct link": "",
        "Source": "Cutshort",
        "Location": "", "Work arrangement": "",
        "Posted date": "", "Verified date": "2026-09-27",
        "Fit": "Excluded",
        "Fit rationale": "Requires 5+ years; beyond profile.",
        "Key gaps": "Experience", "Salary info": "",
        "Resume version": "", "Status": "Excluded — experience", "Submission date": "",
        "Confirmation evidence": "", "Referral status": "None",
        "Responses": "", "Deadlines": "",
        "Next step": "Logged for dedupe only.",
    },
    {
        "Company": "Habit",
        "Role": "SDE-2",
        "Job ID": "",
        "Direct link": "",
        "Source": "Cutshort",
        "Location": "", "Work arrangement": "",
        "Posted date": "", "Verified date": "2026-09-27",
        "Fit": "Excluded",
        "Fit rationale": "Stale listing.",
        "Key gaps": "", "Salary info": "",
        "Resume version": "", "Status": "Excluded — stale", "Submission date": "",
        "Confirmation evidence": "", "Referral status": "None",
        "Responses": "", "Deadlines": "",
        "Next step": "Logged for dedupe only.",
    },
]

GUIDE = [
    ("Status values", ""),
    ("New", "Verified and recorded; not yet actioned."),
    ("Shortlisted", "Worth pursuing; needs verification/tailoring before application."),
    ("Draft saved", "Application started but blocked (e.g. unconfirmed answer); resume when unblocked."),
    ("Ready to submit", "Staged; awaiting an approval card or site-required step."),
    ("Applied", "Submitted with confirmation evidence captured."),
    ("Submission unconfirmed", "Submit attempted but success uncertain — investigate before retrying. NEVER retry blindly."),
    ("Blocked", "Automation blocked (CAPTCHA, anti-bot, login handoff) — link + checklist given to Yugal."),
    ("Interview", "Interview/assessment in progress — see Deadlines/Next step."),
    ("Offer", "Offer received."),
    ("Rejected", "Rejection received."),
    ("Withdrawn", "Withdrawn."),
    ("Excluded — <reason>", "Not pursued: closed / stale / experience mismatch / scam / etc."),
    ("", ""),
    ("Fit values", ""),
    ("High", "Strong overlap, meets autonomous-submit bar when all other conditions hold."),
    ("Medium", "Worthwhile but below the autonomous bar — stage for Yugal's call."),
    ("Low", "Weak fit — logged, not pursued unless asked."),
    ("Excluded", "Not pursued."),
    ("", ""),
    ("Rules", "Update this sheet on EVERY state change. Dedupe by Company + Job ID + Role before adding a row. Never add a duplicate row for the same requisition — update the existing row."),
]

def main():
    if ROOT.joinpath("tracker.xlsx").exists():
        raise SystemExit("Refusing to overwrite existing tracker. Use workflow upsert instead.")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Tracker"
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F4E5F")
    for c, name in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=c, value=name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for r, row in enumerate(ROWS, 2):
        for c, name in enumerate(COLUMNS, 1):
            cell = ws.cell(row=r, column=c, value=row.get(name, ""))
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            # NOTE: keep URLs as plain text — do NOT set cell.hyperlink;
            # hyperlink objects render as "[object Object]" in the viewer.
    widths = [22, 38, 14, 16, 16, 22, 18, 12, 12, 16, 48, 30, 20, 18, 20, 14, 30, 16, 24, 18, 40]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{len(ROWS)+1}"

    g = wb.create_sheet("Guide")
    g["A1"] = "Field / value"
    g["B1"] = "Meaning"
    for cell in (g["A1"], g["B1"]):
        cell.font = header_font
        cell.fill = header_fill
    for r, (a, b) in enumerate(GUIDE, 2):
        g.cell(row=r, column=1, value=a).font = Font(bold=bool(a and not b or a in ("Status values", "Fit values", "Rules")))
        g.cell(row=r, column=2, value=b).alignment = Alignment(wrap_text=True, vertical="top")
    g.column_dimensions["A"].width = 28
    g.column_dimensions["B"].width = 90

    wb.save(PATH)
    print(f"wrote {PATH} with {len(ROWS)} rows")

if __name__ == "__main__":
    main()
