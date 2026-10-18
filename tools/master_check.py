"""Detect drift between the master .tex, its generic PDF and config/candidate.yaml (read-only)."""
import datetime as dt
import re
import shutil
from pathlib import Path
import subprocess
from zoneinfo import ZoneInfo

import candidate
import auto_resume
from local_paths import ROOT

MASTER_PDF=ROOT/'master-resume/Yugal_Teotia_CV.pdf'
LAST_DAY=dt.date(2026,9,30)

def _words(text):
    return re.sub(r'[^a-z0-9]+',' ',text.lower()).split()

def _contains(haystack,needle):
    h=' '.join(_words(haystack));n=' '.join(_words(needle))
    return n in h

def check(today=None):
    today=today or dt.datetime.now(ZoneInfo('Asia/Kolkata')).date()
    parts=auto_resume.master_parts()
    pdf_text=subprocess.run(['pdftotext',str(MASTER_PDF),'-'],check=True,capture_output=True,text=True).stdout
    missing=[b[:90] for j in parts['jobs'] for b in j['bullets'] if not _contains(pdf_text,b)]
    missing+=[b[:90] for p in parts['projects'] for b in p['bullets'] if not _contains(pdf_text,b)]
    missing+=[f'{l}: {i}'[:90] for l,i in parts['skills'] if not _contains(pdf_text,i)]
    ideas=next(j for j in parts['jobs'] if j['org'].startswith('IDeaS'))
    issues=[]
    if missing:issues.append({'kind':'pdf_missing_tex_content','items':missing})
    if today>LAST_DAY and 'present' in ideas['dates'].lower():
        issues.append({'kind':'stale_end_date','detail':'Master shows IDeaS as Present after the last working day (30 Sep 2026); use Sep 2026.'})
    mtime=dt.datetime.fromtimestamp(auto_resume.MASTER.stat().st_mtime);pmtime=dt.datetime.fromtimestamp(MASTER_PDF.stat().st_mtime)
    if mtime>pmtime:issues.append({'kind':'pdf_older_than_tex','tex':mtime.isoformat(timespec='minutes'),'pdf':pmtime.isoformat(timespec='minutes')})
    drift=candidate.drift()
    if drift:issues.append({'kind':'candidate_config_drift','items':drift})
    latex=next((t for t in ('pdflatex','xelatex','lualatex','tectonic') if shutil.which(t)),None) or ('tectonic' if (Path.home()/'.local/bin/tectonic').is_file() else None)
    return {'ok':not issues,'issues':issues,'latex_available':latex,
            'action':None if not issues else ('Run: python3 workflow master-build (then --install --visually-reviewed after inspecting .workflow/master-build/candidate-preview.png). LaTeX engine: '+(latex or 'none; install tectonic or texlive-latex-recommended')+'. The Naukri default resume is refreshed by the next worker restore or a manual upload.')}
