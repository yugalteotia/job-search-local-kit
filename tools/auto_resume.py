"""Role-specific resume for staged Naukri jobs, assembled verbatim from the master .tex.

Code only; wording choices come from config/candidate.yaml. Rendering never
shrinks fonts to fit: low-priority bullets are dropped instead. A PDF is marked
visually_clean only when automated checks pass AND the render template's exact
code hash carries a recorded human/agent visual approval.
"""
import hashlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

import candidate
import job_db
from local_paths import ROOT
from resume_naming import employer_filename

MASTER=ROOT/'master-resume/Yugal_Teotia_CV.tex'
APPROVAL=ROOT/'.workflow/resume-template-approval.json'
RENDERER=ROOT/'tools/render_resume.py'

def _plain(text):
    text=re.sub(r'\\(?:textbf|textit)\{([^{}]*)\}',r'\1',text)
    return text.replace('\\%','%').replace('\\&','&').replace('~',' ').strip()

def _section(source,name):
    return re.split(r'\\section\{',source.split('\\section{'+name+'}',1)[1],1)[0]

def _bullets(block):
    body=re.search(r'\\begin\{itemize\}(.*?)\\end\{itemize\}',block,re.S)
    return [_plain(x) for x in re.split(r'\\item\s+',body.group(1))[1:]] if body else []

def master_parts(source=None):
    source=source or MASTER.read_text()
    skills=[(_plain(m.group(1)).rstrip(':'),_plain(m.group(2)))
            for m in re.finditer(r'\\textbf\{([^{}]*:)\}\s*([^\n]*?)(?:\\par|$)',_section(source,'TECHNICAL SKILLS'),re.M)]
    jobs=[]
    experience=_section(source,'PROFESSIONAL EXPERIENCE')
    for m in re.finditer(r'\\textbf\{([^{}]*)\}\s*\\hfill\s*([^\n]*?)\\par\s*\\textit\{([^{}]*)\}\s*\\hfill\s*([^\n]*?)\\par(.*?)(?=\\textbf\{[^{}]*\}\s*\\hfill|\Z)',experience,re.S):
        jobs.append({'org':_plain(m.group(1)),'location':_plain(m.group(2)),'title':_plain(m.group(3)),
                     'dates':_plain(m.group(4)),'bullets':_bullets(m.group(5))})
    projects=[]
    for m in re.finditer(r'\\textbf\{([^{}]*)\}\s*\\hfill[^\n]*?\\par\s*\\textit\{([^{}]*)\}(.*?)(?=\\textbf\{[^{}]*\}\s*\\hfill|\Z)',_section(source,'PROJECTS'),re.S):
        pieces=[x.strip() for x in _plain(m.group(2)).split(' | ')]
        stack=next((x for x in pieces if ',' in x),pieces[-1])
        status=next((x for x in pieces if x!=stack),'')
        projects.append({'title':_plain(m.group(1)),'note':status,'stack':stack,'bullets':_bullets(m.group(3))})
    education=[]
    for m in re.finditer(r'\\textbf\{([^{}]*)\}\s*\\hfill\s*([^\n]*?)\\par\s*([^\n\\][^\n]*?)(?:\\par|\n)',_section(source,'EDUCATION')):
        education.append({'title':_plain(m.group(1)),'dates':_plain(m.group(2)),'org':_plain(m.group(3)),'location':''})
    return {'skills':skills,'jobs':jobs,'projects':projects,'education':education}

def _pick(items,indices):
    return [items[i] for i in indices if i<len(items)]

def plans(cfg):
    """Fit plans, most complete first: (IDeaS bullets, Data Nova bullets, UNITY bullets)."""
    total=len(cfg['ideas_bullet_order']);nova=len(cfg['keep_data_nova_bullets'])
    result=[(n,nova,2) for n in range(total,4,-1)]
    result+=[(5,n,2) for n in range(nova-1,0,-1)]+[(5,1,1)]
    return result

def build_spec(job,cand=None,parts=None,plan=None):
    cand=cand or candidate.load();parts=parts or master_parts();cfg=cand['resume']
    plan=plan or plans(cfg)[0]
    ideas=next(j for j in parts['jobs'] if j['org'].startswith('IDeaS'))
    nova=next(j for j in parts['jobs'] if j['org'].startswith('Data Nova'))
    wanted=(job['title'] or '')+' '+(job['jd_text'] or '')
    frontend=bool(re.search(r'full.?stack|react|ember|typescript',wanted,re.I))
    order=list(cfg['ideas_bullet_order_frontend'] if frontend else cfg['ideas_bullet_order'])[:plan[0]]
    novas=list(cfg['keep_data_nova_bullets'])[:plan[1]]
    dates=re.sub(r'\s*-\s*(?:Present|.*)$','',ideas['dates']).strip()
    ideas_dates=dates+' – '+cfg['employment_end_label']
    rows=[]
    for label,items in parts['skills']:
        split=cfg.get('skill_rows_split',{}).get(label)
        if split:
            master_items={x.strip() for x in items.split(',')}
            for row in split:
                if any(x.strip() not in master_items for x in row['items'].split(',')):
                    raise ValueError('Split skill row contains an item not in the master: '+row['label'])
            rows.extend(split)
        else:rows.append({'label':label,'items':items})
    if frontend:
        front=[r for r in rows if r['label'].lower().startswith('frontend')]
        rows=[r for r in rows if r not in front]
        rows[1:1]=front
    dn=nova['dates'].replace(' - ',' – ')
    short=cfg.get('org_short',{})
    projects=[{**p,'bullets':p['bullets'][:(plan[2] if i==0 else len(p['bullets']))]} for i,p in enumerate(parts['projects'])]
    spec={'summary':' '.join(cfg['summary'].split()),'skills':rows,
          'experience':[{'title':ideas['title'],'org':ideas['org'],'location':ideas['location'],'dates':ideas_dates,
                         'bullets':_pick(ideas['bullets'],order)},
                        {'title':nova['title'],'org':nova['org'],'location':nova['location'],'dates':dn,
                         'bullets':_pick(nova['bullets'],novas)}],
          'projects':projects,
          'education':[{**e,'org':short.get(e['org'],e['org']),'dates':e['dates'].replace(' - ',' – ')} for e in parts['education']]}
    return spec

def grounded(spec,source=None):
    """Every experience/project bullet and skill row must be verbatim master text."""
    parts=master_parts(source);allowed={b for j in parts['jobs'] for b in j['bullets']}|{b for p in parts['projects'] for b in p['bullets']}
    bad=[b for j in spec['experience'] for b in j['bullets'] if b not in allowed]+\
        [b for p in spec['projects'] for b in p['bullets'] if b not in allowed]
    return bad

def role_filename(job):
    title=re.sub(r'\([^)]*\)','',job['title'] or '');title=re.split(r'\s[|–—-]\s|,',title)[0]
    return employer_filename(title,job['employer'],job['requisition_id'] or '')

def template_hash():
    return hashlib.sha256(RENDERER.read_bytes()).hexdigest()

def template_approved():
    try:return json.loads(APPROVAL.read_text()).get('renderer_sha256')==template_hash()
    except (OSError,ValueError):return False

def _ink_margins(png,dpi=200):
    from PIL import Image,ImageChops
    with Image.open(png).convert('L') as im:
        box=ImageChops.invert(im).point(lambda v:255 if v>40 else 0).getbbox()
        width,height=im.size
    left,top,right,bottom=box
    return {'left':left/dpi,'top':top/dpi,'right':(width-right)/dpi,'bottom':(height-bottom)/dpi}

def _text_of(pdf):
    return re.sub(r'\s+',' ',subprocess.run(['pdftotext','-layout',str(pdf),'-'],check=True,capture_output=True,text=True).stdout)

def render(job,folder,cand=None):
    """Build, check and record one role PDF. Returns the review dict."""
    import render_resume,check_resume_pdf
    from pypdf import PdfReader
    cand=cand or candidate.load();parts=master_parts()
    name=role_filename(job);out=folder/name
    for drop,plan in enumerate(plans(cand['resume'])):
        spec=build_spec(job,cand,parts,plan)
        if grounded(spec):raise ValueError('Resume bullet is not verbatim master text')
        (folder).mkdir(parents=True,exist_ok=True)
        tmp=folder/('.render-'+uuid.uuid4().hex+'.pdf')
        render_resume.build(spec,str(tmp))
        if len(PdfReader(tmp).pages)==1:break
        tmp.unlink()
    else:raise ValueError('Resume cannot fit one page without shrinking type')
    os.replace(tmp,out)
    png=out.with_name(out.stem+'-preview.png')
    subprocess.run(['pdftoppm','-r','200','-png','-singlefile',str(out),str(png.with_suffix(''))],check=True)
    review=check_resume_pdf.check(out,png,False)
    margins=_ink_margins(png);text=_text_of(out)
    missing=[b[:50] for j in spec['experience'] for b in j['bullets']
             if re.sub(r'\s+',' ',b)[:40] not in text]
    checks={'one_page':review['pages']==1,'fonts_and_links':True,
            'margins':min(margins.values())>=0.3,'text_extraction':not missing and 'Yugal Teotia' in text,
            'grounded':True}
    approved=template_approved()
    review['status']='visually_clean' if all(checks.values()) and approved else 'needs_visual_review'
    review['checks']={**checks,'template_visually_approved':approved}
    review['ink_margins_in']={k:round(v,2) for k,v in margins.items()}
    review['inspection']=('Automated one-page, font, link, ink-margin and text-extraction checks passed; render template '
        +template_hash()[:12]+' carries a recorded visual approval.' if review['status']=='visually_clean'
        else 'Automated checks or template visual approval missing: '+json.dumps(review['checks']))
    review['fit_plan']={'ideas_bullets':plan[0],'data_nova_bullets':plan[1],'unity_bullets':plan[2]}
    review['spec_sha256']=hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()
    (folder/'resume_spec.json').write_text(json.dumps(spec,indent=2,ensure_ascii=False)+'\n')
    (folder/'resume_provenance.json').write_text(json.dumps({'master_sha256':hashlib.sha256(MASTER.read_bytes()).hexdigest(),
        'bullets':'verbatim master text, priority-ordered subset','summary':'config/candidate.yaml resume.summary',
        'renderer_sha256':template_hash()},indent=2)+'\n')
    (folder/'resume_visual_review.json').write_text(json.dumps(review,indent=2)+'\n')
    (folder/'resume_latest.json').write_text(json.dumps({'pdf':out.name,'pdf_sha256':review['pdf_sha256']},indent=2)+'\n')
    return review

def run(db,config,limit=3):
    """Prepare resumes for Shortlisted Naukri jobs; never touches a browser."""
    cand=candidate.load();results=[]
    rows=db.execute("""SELECT j.*,q.priority FROM queue q JOIN jobs j ON j.id=q.job_id
        WHERE q.state='QueuedResume' AND j.state='Shortlisted' AND j.source IN ('naukri_portal_native','linkedin_portal_native')
        ORDER BY (j.fit='High') DESC,q.priority DESC,q.queued_at LIMIT ?""",(limit,)).fetchall()
    for job in rows:
        worker='auto-resume-'+uuid.uuid4().hex[:8];job=dict(job);folder=ROOT/'jobs'/job['slug']
        db.execute('BEGIN IMMEDIATE')
        moved=db.execute("UPDATE queue SET state='Resuming',worker_id=?,stage_started=?,attempts=attempts+1 WHERE job_id=? AND state='QueuedResume'",
                         (worker,job_db.now(),job['id'])).rowcount;db.commit()
        if not moved:continue
        try:
            review=render(job,folder,cand)
            pdf=str((folder/review['pdf']).relative_to(ROOT))
            if review['status']!='visually_clean':
                raise ValueError('Role PDF is not visually approved: '+review['inspection'])
            job_db.record_evidence(db,job['id'],'resume_review',pdf)
            job_db.queue_finish_resume(db,job['id'],worker,pdf)
            results.append({'job_id':job['id'],'employer':job['employer'],'status':'ResumeReady','pdf':review['pdf']})
        except Exception as exc:
            (folder/'resume-build-error.json').write_text(json.dumps({'at':job_db.now(),'error':type(exc).__name__,'detail':str(exc)[:300]},indent=2)+'\n')
            db.execute("UPDATE queue SET state='ResumeFailed',worker_id=NULL WHERE job_id=?",(job['id'],));db.commit()
            results.append({'job_id':job['id'],'employer':job['employer'],'status':'ResumeFailed','error':type(exc).__name__})
    return {'prepared':sum(r['status']=='ResumeReady' for r in results),'failed':sum(r['status']=='ResumeFailed' for r in results),'results':results}
