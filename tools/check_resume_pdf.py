"""Check PDF structure and record a visual inspection of its matching PNG.

--visually-reviewed is a human/agent attestation AFTER viewing the PNG; extraction
or this script alone cannot establish visual cleanliness.
"""
from local_paths import ROOT
import argparse, hashlib, json
from pathlib import Path
from pypdf import PdfReader
from PIL import Image

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def check(pdf, png, visually_reviewed=False):
    reader=PdfReader(pdf)
    if len(reader.pages)!=1:
        raise ValueError('Expected one readable page; do not shrink text to force fit')
    page=reader.pages[0]
    fonts=[]
    for value in page['/Resources']['/Font'].values():
        font=value.get_object()
        descriptor=font.get('/FontDescriptor')
        embedded=bool(descriptor and any(k in descriptor.get_object() for k in ['/FontFile','/FontFile2','/FontFile3']))
        if not embedded or not font.get('/ToUnicode'):
            raise ValueError('Every font must be embedded with a Unicode map')
        fonts.append(str(font['/BaseFont']))
    width,height=map(float,(page.mediabox.width,page.mediabox.height))
    links=[]
    for annotation in page.get('/Annots',[]):
        link=annotation.get_object()
        uri=link.get('/A',{}).get('/URI')
        if uri:
            rect=list(map(float,link['/Rect']))
            if not (30<=rect[0]<rect[2]<=width-30 and 30<=rect[1]<rect[3]<=height-30):
                raise ValueError('Link rectangle outside page margins')
            links.append({'uri':str(uri),'rect':rect})
    import candidate
    who=candidate.load()['identity']
    expected={'tel:'+who['phone'],'mailto:'+who['email'],who['linkedin'],who['github']}
    if set(l['uri'] for l in links)!=expected:
        raise ValueError('Contact link targets missing or changed')
    with Image.open(png) as im:
        pixels=list(im.size)
        if min(pixels)<1600:
            raise ValueError('PNG resolution too low for readable review')
    review={'status':'visually_clean' if visually_reviewed else 'needs_visual_review',
            'pdf':pdf.name,'pdf_sha256':digest(pdf),'png':png.name,'png_sha256':digest(png),
            'pages':1,'page_points':[width,height],'png_pixels':pixels,'embedded_fonts':fonts,'links':links,
            'checks':{c:visually_reviewed for c in ['letters','links','margins','bullets']},
            'inspection':'Full actual 200 DPI PNG inspected at original resolution; clear letters and baseline spacing, no collisions, clean indented bullets and margins; contact link labels readable and targets verified' if visually_reviewed else 'Actual PNG must be inspected before approval'}
    return review

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('pdf',type=Path);parser.add_argument('png',type=Path)
    parser.add_argument('--visually-reviewed',action='store_true')
    args=parser.parse_args()
    result=check(args.pdf,args.png,args.visually_reviewed)
    from workflow import write_json,now,event
    result['checked_at']=now()
    spec=args.pdf.parent/'resume_spec.json'
    if spec.exists():result['spec_sha256']=digest(spec)
    write_json(args.pdf.parent/'resume_visual_review.json',result)
    write_json(args.pdf.parent/'resume_latest.json',{'pdf':args.pdf.name,'pdf_sha256':digest(args.pdf)})
    event('resume_visual_review',pdf=str(args.pdf),status=result['status'])
    print(json.dumps(result,indent=2))
