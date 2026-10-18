"""Build the generic master resume PDF from master-resume/*.tex, check it, and install it atomically.

Engines: pdflatex when present, otherwise tectonic (XeTeX; a build copy drops the two pdfTeX-only
unicode-map lines, the source .tex is never modified). The PDF is installed only when every automated
check passes; visual review is a recorded attestation bound to the exact PDF hash.
"""
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import auto_resume
import candidate
import master_check
from local_paths import ROOT

MASTER_TEX=auto_resume.MASTER
MASTER_PDF=master_check.MASTER_PDF
WORK=ROOT/'.workflow/master-build'
PDFTEX_ONLY=('\\input{glyphtounicode}','\\pdfgentounicode=1')
MIN_INK_MARGIN_IN=0.25

def engine():
    for name in ('pdflatex',):
        if shutil.which(name):return name
    for candidate_path in (shutil.which('tectonic'),str(Path.home()/'.local/bin/tectonic')):
        if candidate_path and Path(candidate_path).is_file():return 'tectonic:'+candidate_path
    return None

XETEX_FONTS=r"""\usepackage{fontspec}
\setmainfont[Extension=.otf,UprightFont=*10-regular,BoldFont=*10-bold,ItalicFont=*10-italic,BoldItalicFont=*10-bolditalic,Ligatures=NoCommon]{lmroman}
\setsansfont[Extension=.otf,UprightFont=*10-regular,BoldFont=*10-bold,ItalicFont=*10-oblique,BoldItalicFont=*10-boldoblique,Ligatures=NoCommon]{lmsans}
"""
PDFTEX_FONTS='\\usepackage[T1]{fontenc}\n\\usepackage{lmodern}\n'

def build_copy(source):
    """Text of the XeTeX build copy. Same document and design (Latin Modern Sans), with two differences:
    the pdfTeX-only unicode-map lines are dropped, and Latin Modern OpenType is loaded with common ligatures
    disabled so ATS text extraction yields 'workflows'/'Buffers', never 'workﬂows'/'Buﬀers'."""
    text=''.join(line for line in source.splitlines(True) if line.strip() not in PDFTEX_ONLY)
    if PDFTEX_FONTS.replace('\\\\','\\') not in text:raise ValueError('Master preamble changed: update the XeTeX font shim')
    return text.replace(PDFTEX_FONTS.replace('\\\\','\\'),XETEX_FONTS,1)

def _compile(tex_text,workdir,name):
    kind=engine()
    if not kind:raise RuntimeError('No LaTeX engine: install texlive-latex-recommended or tectonic')
    tex=Path(workdir)/(name+'.tex')
    if kind=='pdflatex':
        tex.write_text(tex_text)
        for _ in range(2):
            done=subprocess.run(['pdflatex','-interaction=nonstopmode','-halt-on-error',tex.name],cwd=workdir,capture_output=True,text=True)
            if done.returncode:raise RuntimeError('pdflatex failed: '+done.stdout[-400:])
    else:
        tex.write_text(build_copy(tex_text))
        done=subprocess.run([kind.split(':',1)[1],'-X','compile',tex.name],cwd=workdir,capture_output=True,text=True)
        if done.returncode:raise RuntimeError('tectonic failed: '+(done.stderr or done.stdout)[-400:])
    return Path(workdir)/(name+'.pdf'),kind

def _ink_margins(png,dpi=200):
    from PIL import Image,ImageChops
    with Image.open(png).convert('L') as im:
        box=ImageChops.invert(im).point(lambda v:255 if v>40 else 0).getbbox();width,height=im.size
    return {'left':box[0]/dpi,'top':box[1]/dpi,'right':(width-box[2])/dpi,'bottom':(height-box[3])/dpi}

def checks(pdf,png):
    from pypdf import PdfReader
    reader=PdfReader(pdf);page=reader.pages[0];width,height=map(float,(page.mediabox.width,page.mediabox.height))
    text=subprocess.run(['pdftotext',str(pdf),'-'],capture_output=True,text=True,check=True).stdout
    parts=auto_resume.master_parts(MASTER_TEX.read_text())
    missing=[b[:80] for j in parts['jobs'] for b in j['bullets'] if not master_check._contains(text,b)]
    missing+=[b[:80] for p in parts['projects'] for b in p['bullets'] if not master_check._contains(text,b)]
    missing+=[f'{l}: {i}'[:80] for l,i in parts['skills'] if not master_check._contains(text,i)]
    who=candidate.load()['identity']
    uris={str(a.get_object().get('/A',{}).get('/URI')) for a in page.get('/Annots',[])}
    expected={'tel:'+who['phone'],'mailto:'+who['email'],who['linkedin'],who['github']}
    fonts=[]
    for value in page['/Resources']['/Font'].values():
        font=value.get_object()
        if '/DescendantFonts' in font:font=font['/DescendantFonts'][0].get_object()   # CID/Type0 fonts keep the descriptor here
        descriptor=font.get('/FontDescriptor')
        fonts.append(bool(descriptor and any(k in descriptor.get_object() for k in ('/FontFile','/FontFile2','/FontFile3'))))
    margins=_ink_margins(png)
    result={'one_page':len(reader.pages)==1,'a4':abs(width-595.28)<1 and abs(height-841.89)<1,
            'all_master_text_present':not missing,'contact_links':expected<=uris,'fonts_embedded':bool(fonts) and all(fonts),
            'ink_margins_ok':min(margins.values())>=MIN_INK_MARGIN_IN,'no_ligature_glyphs_in_text':not re.search('[\ufb00-\ufb06]',text),'not_stale_present_after_last_day':True}
    ideas=next(j for j in parts['jobs'] if j['org'].startswith('IDeaS'))
    if dt.date.today()>master_check.LAST_DAY and 'present' in ideas['dates'].lower():result['not_stale_present_after_last_day']=False
    return result,{k:round(v,2) for k,v in margins.items()},missing

def build(install=False,visually_reviewed=False):
    WORK.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        source=MASTER_TEX.read_text();name='Yugal_Teotia_CV'
        pdf,kind=_compile(source,tmp,name)
        staged=WORK/'candidate.pdf';shutil.copy2(pdf,staged)
    png=WORK/'candidate-preview.png'
    subprocess.run(['pdftoppm','-r','200','-png','-singlefile',str(staged),str(png.with_suffix(''))],check=True)
    result,margins,missing=checks(staged,png)
    digest=hashlib.sha256(staged.read_bytes()).hexdigest()
    review={'built_at':dt.datetime.now(dt.timezone.utc).isoformat(),'engine':kind.split(':')[0],'pdf':'candidate.pdf','pdf_sha256':digest,
            'png':png.name,'tex_sha256':hashlib.sha256(source.encode()).hexdigest(),'checks':result,'ink_margins_in':margins,
            'missing_text':missing,'automated_ok':all(result.values()),'visually_reviewed':bool(visually_reviewed),
            'installed':False}
    previous=None
    try:previous=json.loads((WORK/'review.json').read_text())
    except (OSError,ValueError):pass
    if not visually_reviewed and previous and previous.get('pdf_sha256')==digest and previous.get('visually_reviewed'):
        review['visually_reviewed']=True   # same bytes as an already attested build
    if install:
        if not review['automated_ok']:raise ValueError('Automated checks failed; not installing: '+json.dumps(result))
        if not review['visually_reviewed']:raise ValueError('Visual review has not been recorded for this exact PDF (use --visually-reviewed after inspecting the PNG)')
        backups=ROOT/'.workflow/backups';backups.mkdir(exist_ok=True)
        if MASTER_PDF.is_file():
            shutil.copy2(MASTER_PDF,backups/f'Yugal_Teotia_CV-{hashlib.sha256(MASTER_PDF.read_bytes()).hexdigest()[:8]}-{dt.date.today():%Y%m%d}.pdf')
        tmp_target=MASTER_PDF.with_suffix('.pdf.tmp');shutil.copy2(staged,tmp_target);tmp_target.replace(MASTER_PDF)
        review['installed']=True
    (WORK/'review.json').write_text(json.dumps(review,indent=2)+'\n')
    return review
