"""Preview-first cleanup of explicitly terminal requisitions."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import re

MINIMAL=('Company','Role','Job ID','Direct link','Status','Status date','Known reason')
PROOF_NAMES={
    'confirmation.json','submission-confirmation.md',
    'submission-confirmation-snapshot.txt','submission-status-snapshot.txt',
    'submission-status.png',
}
RETENTION_DAYS=30

def is_terminal(status):
    s=str(status or '').casefold().strip()
    return bool(re.match(r'^(?:rejected|closed|expired|withdrawn)(?:$|[\s—:-])',s) or
                re.match(r'^excluded\s*[—-]\s*(?:closed|expired)(?:$|[\s—:-])',s))

def observed_date(row):
    if row.get('Status date'): return str(row['Status date'])[:10]
    status=str(row.get('Status') or '').casefold()
    if status=='rejected' and row.get('Submission date'):
        responses=str(row.get('Responses') or '')
        # A submission date alone is not a rejection date.
        m=re.search(r'updated\s+([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})',responses,re.I)
        if m:
            try: return dt.datetime.strptime(' '.join(m.groups()),'%b %d %Y').date().isoformat()
            except ValueError: pass
    if 'closed' in status or 'expired' in status:
        checked=str(row.get('Verified date') or '')[:10]
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}',checked): return checked
    return 'unknown'

def minimal(row):
    return {'Company':row.get('Company'),'Role':row.get('Role'),'Job ID':row.get('Job ID'),
            'Direct link':row.get('Direct link'),'Status':row.get('Status'),
            'Status date':observed_date(row),'Known reason':'reason unknown'}

def matches_folder(w,row,folder):
    listing=w.read_json(folder/'listing.json',{})
    if listing:
        candidate={'Company':listing.get('company'),'Role':listing.get('role'),'Job ID':listing.get('job_id'),'Direct link':listing.get('url')}
        return w.key(candidate)==w.key(row)
    req=w.normalize(row.get('Job ID'))
    return bool(req and req in w.normalize(folder.name))

def exclusive_capture(w,path,row):
    name=path.name.casefold()
    req=w.normalize(row.get('Job ID'))
    if req and req in w.normalize(name): return True
    # Name-only captures are accepted only when the file itself names this
    # requisition and no other tracked requisition. Shared history is retained.
    if not (name.startswith('optum-') or name in ('appian-listing.json',)):
        return False
    try: content=path.read_text()
    except (OSError,UnicodeDecodeError): return False
    if not req or req not in w.normalize(content): return False
    _,_,_,all_rows=w.tracker_rows()
    return not any(w.key(other)!=w.key(row) and w.normalize(other.get('Job ID')) and w.normalize(other.get('Job ID')) in w.normalize(content) for other in all_rows)

def fingerprint(w,path):
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(w.ROOT.resolve()):
        raise ValueError('Cleanup target must be a regular workspace file')
    return {'path':str(path.relative_to(w.ROOT)),'bytes':path.stat().st_size,
            'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

def plan(w,at=None):
    current=at or dt.datetime.now(dt.timezone.utc)
    _,_,_,rows=w.tracker_rows();selected=[r for r in rows if is_terminal(r.get('Status'))]
    delete=[];retain=[];keys=[];seen=set()
    for row in selected:
        keys.append(minimal(row))
        folders=[p for p in (w.ROOT/'jobs').iterdir() if p.is_dir() and matches_folder(w,row,p)] if (w.ROOT/'jobs').exists() else []
        for folder in folders:
            proof= w.read_json(folder/'confirmation.json',{})
            captured=proof.get('captured_at') if isinstance(proof,dict) else None
            due=None
            if captured:
                try: due=dt.datetime.fromisoformat(captured.replace('Z','+00:00'))+dt.timedelta(days=RETENTION_DAYS)
                except ValueError: pass
            for path in sorted(folder.rglob('*')):
                if not path.is_file(): continue
                rel=str(path.relative_to(w.ROOT))
                if rel in seen: continue
                seen.add(rel)
                fp=fingerprint(w,path)
                if path.name in PROOF_NAMES and due and current<due:
                    retain.append({**fp,'until':due.isoformat(),'purpose':'submission/status confirmation evidence'})
                elif path.name in PROOF_NAMES and not due:
                    retain.append({**fp,'until':'unknown — review proof date before pruning','purpose':'confirmation evidence; date uncertain'})
                else: delete.append({**fp,'reason':'terminal job artifact or expired proof'})
        for path in sorted((w.ROOT/'captures').glob('*.json')):
            rel=str(path.relative_to(w.ROOT))
            if rel not in seen and exclusive_capture(w,path,row):
                seen.add(rel);delete.append({**fingerprint(w,path),'reason':'terminal-only capture'})
    delete.sort(key=lambda x:x['path']);retain.sort(key=lambda x:x['path'])
    other_history=[{k:r.get(k) for k in MINIMAL} for r in w.read_json(w.STATE/'cleanup/other_history_keys.json',[])]
    snapshot=w.ROOT/'jobs/optum-2388999-java-fullstack/submission-status-snapshot.txt'
    if snapshot.is_file():
        body=snapshot.read_text()
        if ('Job Number: 2376740' in body and 'Senior Software Engineer I' in body and
            'No longer under consideration' in body and 'Updated: Sep 8, 2026' in body):
            record={'Company':'UnitedHealth Group (Optum)','Role':'Senior Software Engineer I',
                                  'Job ID':'2376740','Direct link':None,'Status':'Rejected',
                                  'Status date':'2026-09-08','Known reason':'reason unknown'}
            if not any(w.key(r)==w.key(record) for r in other_history): other_history.append(record)
    result={'created_at':current.isoformat(),'terminal_rows':keys,'other_history_keys':other_history,'delete_now':delete,'retain_temporarily':retain,
            'total_bytes_to_delete':sum(f['bytes'] for f in delete),'policy':'See TERMINAL_CLEANUP_POLICY.md'}
    return result

def preview(w):
    result=plan(w)
    path=w.STATE/'cleanup/preview.json';w.write_json(path,result)
    lines=['# Terminal cleanup preview','',f"Delete now: {len(result['delete_now'])} files, {result['total_bytes_to_delete']} bytes.",'',
           '## Minimal tracker rows', '']
    for row in result['terminal_rows']:
        lines.append('- '+ ' | '.join(str(row.get(k) or '—') for k in MINIMAL))
    lines+=['','## Exact files to delete now','']
    lines += [f"- `{f['path']}` — {f['bytes']} bytes; SHA256 {f['sha256']}" for f in result['delete_now']]
    lines+=['','## Confirmation proof retained temporarily','']
    lines += [f"- `{f['path']}` — until {f['until']}" for f in result['retain_temporarily']]
    lines+=['','## Other historical deduplication key extracted before proof pruning','']
    lines += [f"- {r['Company']} | {r['Role']} | {r['Job ID']} | {r['Status']} | {r['Status date']} | {r['Known reason']}" for r in result['other_history_keys']]
    lines+=['','Shared captures, tracker backups and global audit history are outside this deletion set.','']
    (w.STATE/'cleanup/preview.md').write_text('\n'.join(lines))
    return {'preview':str(path.relative_to(w.ROOT)),'readable_preview':'.workflow/cleanup/preview.md',
            'terminal_rows':len(result['terminal_rows']),'delete_files':len(result['delete_now']),
            'delete_bytes':result['total_bytes_to_delete'],'retained_proof_files':len(result['retain_temporarily'])}

def apply(w):
    w.check_pause()
    saved=w.read_json(w.STATE/'cleanup/preview.json')
    if not saved: raise ValueError('Run cleanup-preview first')
    current=plan(w)
    if current['terminal_rows']!=saved['terminal_rows'] or current['other_history_keys']!=saved['other_history_keys'] or current['delete_now']!=saved['delete_now'] or current['retain_temporarily']!=saved['retain_temporarily']:
        raise ValueError('Cleanup preview changed; regenerate and review before applying')
    wb,ws,cols,rows=w.tracker_rows()
    for name in MINIMAL:
        if name not in cols:
            ws.cell(1,len(cols)+1,name);cols.append(name)
    changed=0
    for index,row in enumerate(rows,2):
        if not is_terminal(row.get('Status')): continue
        keep=minimal(row)
        for col,name in enumerate(cols,1):
            value=keep.get(name)
            if ws.cell(index,col).value!=value:
                cell=ws.cell(index,col)
                cell.value=value
                if isinstance(value,str): cell.data_type='s'
                changed+=1
    with w.locked():
        backup=w.STATE/'backups'/('pre-terminal-cleanup-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')+'.xlsx')
        backup.parent.mkdir(exist_ok=True)
        import shutil
        shutil.copy2(w.ROOT/'tracker.xlsx',backup)
        tmp=w.STATE/'cleanup/tracker.tmp.xlsx';wb.save(tmp);tmp.replace(w.ROOT/'tracker.xlsx')
    # Verify the durable dedup/history keys before deleting any artifact.
    after=w.tracker_rows()[3]
    for key in saved['terminal_rows']:
        matches=[r for r in after if w.key(r)==w.key(key)]
        if len(matches)!=1 or any(matches[0].get(k)!=key.get(k) for k in MINIMAL):
            raise ValueError('Tracker key verification failed; files were not deleted')
        if any(matches[0].get(k) is not None for k in cols if k not in MINIMAL):
            raise ValueError('Terminal tracker row not minimal; files were not deleted')
    retain_state=w.STATE/'cleanup/retention.json'
    w.write_json(retain_state,{'files':saved['retain_temporarily'],'days':RETENTION_DAYS})
    w.write_json(w.STATE/'cleanup/other_history_keys.json',saved['other_history_keys'])
    removed=[]
    for record in saved['delete_now']:
        path=w.ROOT/record['path']
        if fingerprint(w,path)!={k:record[k] for k in ('path','bytes','sha256')}:
            raise ValueError('Cleanup target changed before deletion; stop and re-preview')
        path.unlink();removed.append(record['path'])
    w.event('terminal_cleanup',rows=len(saved['terminal_rows']),deleted_files=removed,backup=str(backup.relative_to(w.ROOT)))
    import tracker_views
    views=tracker_views.views(w)
    if not (w.STATE/'cleanup/first-applied.json').exists():
        import shutil
        shutil.copy2(w.STATE/'cleanup/preview.md',w.STATE/'cleanup/first-preview.md')
        shutil.copy2(w.STATE/'cleanup/preview.json',w.STATE/'cleanup/first-preview.json')
        w.write_json(w.STATE/'cleanup/first-applied.json',{'applied_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                 'initial_preview':'.workflow/cleanup/first-preview.md'})
    return {'terminal_rows':len(saved['terminal_rows']),'cleared_cells':changed,'deleted_files':removed,
            'retained_proof_files':len(saved['retain_temporarily']),'tracker_backup':str(backup.relative_to(w.ROOT)),
            'views':views}

def prune_retained(w,at=None):
    state_path=w.STATE/'cleanup/retention.json';state=w.read_json(state_path,{'files':[]})
    current=at or dt.datetime.now(dt.timezone.utc);kept=[];removed=[]
    for record in state['files']:
        try: due=dt.datetime.fromisoformat(record['until'])
        except (ValueError,KeyError): kept.append(record);continue
        if current<due: kept.append(record);continue
        path=w.ROOT/record['path']
        if not path.exists(): continue
        if fingerprint(w,path)!={k:record[k] for k in ('path','bytes','sha256')}:
            kept.append(record);continue
        path.unlink();removed.append(record['path'])
    if removed or len(kept)!=len(state['files']):
        w.write_json(state_path,{'files':kept,'days':RETENTION_DAYS})
        w.event('terminal_proof_pruned',files=removed)
    return {'pruned':removed,'retained':len(kept)}

def after_normal_run(w):
    """After the reviewed first cleanup, apply future terminal changes and prune due proof."""
    if not (w.STATE/'cleanup/first-applied.json').exists():
        saved=w.read_json(w.STATE/'cleanup/preview.json',{})
        events=(w.STATE/'events.jsonl').read_text() if (w.STATE/'events.jsonl').is_file() else ''
        _,_,cols,rows=w.tracker_rows()
        already_applied=bool(saved.get('terminal_rows') and '"event": "terminal_cleanup"' in events and
            all(not (w.ROOT/f['path']).exists() for f in saved.get('delete_now',[])) and
            all(any(w.key(r)==w.key(key) and all(r.get(k)==key.get(k) for k in MINIMAL) and
                    all(r.get(k) is None for k in cols if k not in MINIMAL) for r in rows)
                for key in saved['terminal_rows']))
        if already_applied:
            records=[json.loads(line) for line in events.splitlines() if '"event": "terminal_cleanup"' in line]
            import shutil
            shutil.copy2(w.STATE/'cleanup/preview.md',w.STATE/'cleanup/first-preview.md')
            shutil.copy2(w.STATE/'cleanup/preview.json',w.STATE/'cleanup/first-preview.json')
            w.write_json(w.STATE/'cleanup/first-applied.json',{'applied_at':records[-1]['time'],
                       'initial_preview':'.workflow/cleanup/first-preview.md','reconciled_from_existing_audit':True})
    if not (w.STATE/'cleanup/first-applied.json').exists() or w.read_json(w.STATE/'control.json',{}).get('paused'):
        return {'status':'First preview not applied or workflow paused'}
    pruned=prune_retained(w)
    _,_,cols,rows=w.tracker_rows()
    dirty=any(is_terminal(r.get('Status')) and (any(r.get(k) is not None for k in cols if k not in MINIMAL)
                    or not r.get('Status date') or not r.get('Known reason')) for r in rows)
    if not dirty: return {'pruned':pruned,'cleanup':'No new terminal rows'}
    summary=preview(w)
    applied=apply(w)
    return {'pruned':pruned,'preview':summary,'applied':applied}
