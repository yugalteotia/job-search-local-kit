"""Read-only tracker projections and conservative, review-first historical matching."""
import datetime as dt
import hashlib
import html
import json
import re
from pathlib import Path

QUICK = ['Company','Role','Location','Applied date','Status','Last response','Next action','Direct link']

def active(row):
    status = str(row.get('Status') or '').lower()
    closed = any(s in status for s in ['rejected','withdrawn','closed','expired','excluded','stopped'])
    application = any(s in status for s in ['shortlisted','in progress','inprogress','prepared','blocked','needs answer','draft saved','applied','interview','offer','submission unconfirmed','submissionunconfirmed','uncertain','needsreview','ready to submit'])
    urgent = any(s in str(row.get('Next step') or '').lower() for s in ['urgent','deadline','follow up','follow-up','respond'])
    return not closed and (application or urgent)

def views(w, all_rows=False, rows_override=None, columns_override=None):
    if rows_override is None:
        _,_,cols,rows=w.tracker_rows()
    else:
        rows=rows_override;cols=columns_override or list(dict.fromkeys(k for row in rows for k in row))
    folder=w.ROOT/'views';folder.mkdir(exist_ok=True)
    display_rows=[]
    for row in rows:
        display=dict(row)
        # A stopped application remains stopped in the primary tracker/history.
        # Present its unresolved work as Blocked in the quick view without
        # changing authorization or allowing preparation to resume.
        if str(row.get('Status') or '').lower()=='stopped':
            for marker in (w.ROOT/'jobs').glob('*/application_stopped.json'):
                listing=w.read_json(marker.parent/'listing.json',{})
                if w.key(row)==w.key({'Company':listing.get('company'),'Job ID':listing.get('job_id'),'Role':listing.get('role'),'Direct link':listing.get('url')}):
                    display['Status']='Blocked'
                    display['Next step']=row.get('Next step') or 'Stopped by user; explicit authorization required before resuming'
                    break
        if row.get('Status')=='SubmissionUnconfirmed':
            display['Status']='Uncertain (locked)'
            display['Next step']=row.get('Next step') or 'Investigate exact submission proof; do not retry'
        display_rows.append(display)
    selected=[r for r in display_rows if all_rows or active(r)]
    selected.sort(key=lambda r: (not any(s in str(r.get('Next step') or '').lower() for s in ['urgent','deadline','respond']),str(r.get('Company'))))
    quick=[{k:((str(r.get('Responses') or '').splitlines() or [''])[-1] if k=='Last response' else r.get('Next step') if k=='Next action' else r.get('Submission date',r.get('Applied date')) if k=='Applied date' else r.get(k)) for k in QUICK} for r in selected]
    # Detailed snapshot retains all columns; evidence files and the complete audit remain on disk.
    detailed={'rows':rows,'columns':cols,'events':(w.STATE/'events.jsonl').read_text() if (w.STATE/'events.jsonl').exists() else '',
              'changes':[{ 'file':str(p.relative_to(w.ROOT)), 'data':w.read_json(p)} for p in sorted((w.STATE/'changes').glob('*.json'))],
              'evidence_files':[str(p.relative_to(w.ROOT)) for p in (w.ROOT/'jobs').rglob('*') if p.is_file()]}
    w.write_json(folder/'quick.json',quick);w.write_json(folder/'detailed.json',detailed)
    def table(records,columns):
        def cell(k,value):
            text=html.escape(str(value or ''))
            if k=='Direct link' and str(value).startswith(('https://','http://')):
                return '<a href="'+html.escape(str(value),quote=True)+'">Open listing</a>'
            return text
        return '<table><thead><tr>'+''.join('<th>'+html.escape(k)+'</th>' for k in columns)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+cell(k,r.get(k))+'</td>' for k in columns)+'</tr>' for r in records)+'</tbody></table>'
    css='<meta charset="utf-8"><style>body{font:15px sans-serif;margin:24px}table{border-collapse:collapse}th,td{padding:10px;border:1px solid #ddd;text-align:left;vertical-align:top}th{background:#eee}td{max-width:380px;overflow-wrap:anywhere}pre{white-space:pre-wrap}</style>'
    (folder/'quick.html').write_text(css+'<h1>Quick tracker</h1><p>'+('All rows' if all_rows else 'Shortlisted, prepared, blocked, active applications and urgent actions')+f' · {len(quick)} rows</p><p>Stopped application work is shown as Blocked here; the detailed tracker preserves the stop instruction.</p><p><a href="detailed.html">Detailed view</a></p>'+table(quick,QUICK))
    (folder/'detailed.html').write_text(css+'<h1>Detailed tracker</h1>'+table(rows,cols)+'<h2>Evidence files</h2><pre>'+html.escape('\n'.join(detailed['evidence_files']))+'</pre><h2>History and audit</h2><pre>'+html.escape(json.dumps(detailed['changes'],indent=2,default=str)+'\n'+detailed['events'])+'</pre>')
    return {'quick_rows':len(quick),'detailed_rows':len(rows),'quick':'views/quick.html','detailed':'views/detailed.html'}

def preview(w,path):
    capture=w.read_json(path)
    records=capture['records']
    _,_,cols,rows=w.tracker_rows()
    proposals=[];seen=set()
    for raw in records:
        r=dict(raw)
        ident=hashlib.sha256(json.dumps(r,sort_keys=True).encode()).hexdigest()[:16]
        company=w.employer(r.get('company'));role=w.normalize(r.get('role'));req=w.normalize(r.get('requisition_id'))
        date=r.get('date','')
        valid_date=bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}',date))
        if valid_date:
            try: dt.date.fromisoformat(date)
            except ValueError: valid_date=False
        evidence=w.ROOT/str(r.get('evidence',''))
        evidence_ok=evidence.resolve().is_relative_to(w.ROOT) and evidence.is_file()
        kind=r.get('kind')
        matched=[i+2 for i,x in enumerate(rows) if company==w.employer(x.get('Company')) and req and req==w.normalize(x.get('Job ID'))]
        loose=[i+2 for i,x in enumerate(rows) if company==w.employer(x.get('Company')) and role and role==w.normalize(x.get('Role'))]
        signature=(company,req) if req else (company,role,date)
        if kind in ['alert','recommendation','saved_job']:
            decision,reason='ignore','Alert, recommendation or saved job is not an application'
        elif matched:
            decision,reason='existing','Same company and requisition already tracked; never reapply'
        elif signature in seen:
            decision,reason='duplicate','Duplicate captured history record'
        elif loose or not (company and role and req and valid_date and evidence_ok) or kind not in ['application_acknowledgment','portal_application','recruiter_response']:
            decision,reason='review','Ambiguous identity/date, missing requisition/evidence, or uncertain application event'
        elif kind=='recruiter_response':
            decision,reason='review','Recruiter message alone does not prove an application'
        else:
            decision,reason='proposed','Company, role, requisition and date supplied with application evidence; review before import'
        if decision not in ['ignore','review']:
            seen.add(signature)
        proposals.append({'id':ident,'decision':decision,'reason':reason,'matching_rows':matched or loose,'record':r,'evidence_sha256':hashlib.sha256(evidence.read_bytes()).hexdigest() if evidence_ok else None})
    result={'created':w.now(),'source':str(path),'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'coverage':capture.get('coverage',{}),'proposals':proposals,'imported':False}
    w.write_json(w.STATE/'historical-preview.json',result)
    text='# Historical backfill preview\n\nNo rows imported. Ambiguous items require review; alerts are ignored.\n\n'
    for p in proposals:
        text+='- '+p['decision']+' · '+json.dumps(p['record'],ensure_ascii=False)+' — '+p['reason']+'\n'
    text+='\nCoverage:\n```json\n'+json.dumps(result['coverage'],indent=2)+'\n```\n'
    (w.ROOT/'views').mkdir(exist_ok=True)
    (w.ROOT/'views/historical-preview.md').write_text(text)
    w.event('historical_preview',source=str(path),counts={k:sum(p['decision']==k for p in proposals) for k in ['proposed','review','existing','ignore','duplicate']})
    return result

def import_reviewed(w,review_path):
    # Explicit approved record IDs only; no bulk acceptance of ambiguous records.
    w.check_pause()
    if w.read_json(w.STATE/'historical-imported.json'):
        raise ValueError('One-time historical import already completed')
    result=w.read_json(w.STATE/'historical-preview.json',{})
    approval=w.read_json(review_path)
    if approval.get('preview_sha256')!=hashlib.sha256((w.STATE/'historical-preview.json').read_bytes()).hexdigest():
        raise ValueError('Approval must identify the exact preview SHA256')
    source=Path(result['source'])
    if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=result['source_sha256']:
        raise ValueError('Historical source changed: regenerate preview')
    ids=approval.get('approved_ids',[])
    if not ids or len(ids)!=len(set(ids)):
        raise ValueError('Supply distinct explicitly reviewed proposed IDs')
    selected=[p for p in result['proposals'] if p['id'] in ids and p['decision']=='proposed']
    if len(selected)!=len(ids):raise ValueError('Only proposed records can be imported; resolve ambiguity first')
    _,_,_,rows=w.tracker_rows()
    for p in selected:
        r=p['record']
        if any(w.employer(x.get('Company'))==w.employer(r['company']) and w.normalize(x.get('Job ID'))==w.normalize(r['requisition_id']) for x in rows):
            raise ValueError('Tracker changed: regenerate preview before importing')
        evidence=w.ROOT/r['evidence']
        if not evidence.is_file() or hashlib.sha256(evidence.read_bytes()).hexdigest()!=p['evidence_sha256']:raise ValueError('Application evidence missing or changed')
    for p in selected:
        r=p['record']
        _,_,cols,_=w.tracker_rows()
        date_column='Submission date' if 'Submission date' in cols else 'Applied date'
        row={'Company':r['company'],'Role':r['role'],'Job ID':r['requisition_id'],'Direct link':r.get('url'),'Location':r.get('location'),date_column:r['date'],'Status':r.get('status') if r.get('status') in ['Rejected','Interview','Offer','Withdrawn'] else 'Applied','Confirmation evidence':r['evidence'],'Source':r.get('source'),'Responses':r.get('response'),'Next step':'Historical application; do not reapply; review current status'}
        w.upsert(row,'explicitly reviewed historical import')
    w.write_json(w.STATE/'historical-imported.json',{'at':w.now(),'ids':ids})
    return {'imported':len(ids)}
