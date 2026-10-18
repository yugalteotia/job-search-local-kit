"""`workflow apply-now`: the scheduled pipeline, started immediately.

This module contains no pipeline logic. It calls `pipeline_v2.run(scheduled=True)`,
the same function the systemd service reaches through `workflow daily --scheduled`,
so configuration, the run lock, fit rules, resume checks, the one-claim limit,
answer handling, proof gates, alerts and resume restoration are shared. `--dry-run`
maps to the pipeline's existing test mode (Naukri worker reports WouldSubmit and
opens no form).
"""
import os
import re
import shutil
import sys
import time
from pathlib import Path

import core_run
import job_db
from local_paths import ROOT

REFUSED=('Paused','Disabled','OverlapPrevented')

def run(dry_run=False,stream=None,**options):
    stream=stream or sys.stderr
    started=time.monotonic()
    def progress(message):
        print(f'[{time.monotonic()-started:5.0f}s] {message}',file=stream,flush=True)
    import job_config
    live=any(job_config.live_provider_enabled(job_config.load(),p,test_mode=dry_run) for p in ('naukri','linkedin'))
    progress('Starting '+('configured Naukri/LinkedIn live run (provider caps apply)' if live else 'read-only run (no uploads, answers or submissions)'))
    return core_run.execute(dry_run=dry_run,trigger=os.environ.get('JOB_SEARCH_TRIGGER','cli'),progress=progress,**options)

def _evidence(job_id,db):
    row=db.execute('SELECT slug,canonical_url,employer,title FROM jobs WHERE id=?',(job_id,)).fetchone()
    if not row:return {}
    folder=ROOT/'jobs'/row['slug']
    files=[str(p.relative_to(ROOT)) for p in sorted(list(folder.glob('naukri-native-*.json'))+list(folder.glob('linkedin-easy-apply-*.json')))]
    return {'employer':row['employer'],'title':row['title'],'link':row['canonical_url'],'evidence':files}

def summarize(result):
    """Plain-text final summary from the pipeline result and saved evidence."""
    if result.get('status') in REFUSED:
        reason={'OverlapPrevented':'another run is active','Paused':'workflow is paused','Disabled':'schedule is disabled in config'}[result['status']]
        return f"Not started: {reason}. {result.get('reason','')}".strip(),False
    lines=[];db=job_db.connect()
    try:
        sources=result.get('sources',{})
        ok=[k for k,v in sources.items() if v.get('status')=='Success']
        bad={k:v for k,v in sources.items() if v.get('status')!='Success'}
        lines.append(f"Sources checked: {len(sources)} ({len(ok)} OK)"+(''.join(f"; {k}: {v.get('status')}{' '+str(v.get('error_kind')) if v.get('error_kind') else ''}" for k,v in bad.items())))
        sc=result.get('stage_counts',{})
        af=result.get('auto_fit') or {}
        lines.append(f"Roles reviewed: {sc.get('original_or_portal_jds_reviewed',0)} new JDs + {af.get('considered',0)} saved native-portal fit reviews; queued {af.get('queued',0)}, not eligible {af.get('not_eligible',0)}, blocked {af.get('blocked',0)}")
        worker=result.get('naukri_application_worker',{})
        linked=result.get('linkedin_application_worker',{})
        lines.append(f"LinkedIn worker: {linked.get('status','not run')}, candidates {linked.get('candidate_count',0)}")
        for provider,report in (('Naukri',worker),('LinkedIn',linked)):
            if 'new_attempts_used' in report:lines.append(f"{provider} new attempts: {report['new_attempts_used']}/{report['max_new_attempts_per_run']}; new claims {report['new_claims_created']}; existing continuations {report['existing_attempts_continued']}")
        worker={**worker,'checks':list(worker.get('checks',[]) or [])+list(linked.get('checks',[]) or []),
                'results':list(worker.get('results',[]) or [])+list(linked.get('results',[]) or [])}
        lines.append(f"Naukri worker: {worker.get('status','not run')}"+(' (DRY RUN)' if result.get('test') else '')+f", candidates {worker.get('candidate_count',0)}")
        confirmed=[];blockers=[]
        for check in worker.get('checks',[]) or []:
            info=_evidence(check['job_id'],db)
            lines.append(f"  would submit: {info.get('employer')} - {info.get('title')} with {check.get('resume_filename')}  {info.get('link','')}")
        for item in worker.get('results',[]) or []:
            info=_evidence(item['job_id'],db)
            label=f"{info.get('employer')} - {info.get('title')} [{item.get('status')}]"
            if item.get('status')=='Applied':confirmed.append((label,item.get('confirmation_path'),info))
            else:blockers.append((label,item,info))
        reconciled={item['job_id']:item for report in result.get('application_reconciliation',{}).values()
                    for item in report.get('confirmed',[])}
        if reconciled:
            blockers=[entry for entry in blockers if entry[1].get('job_id') not in reconciled]
            existing={entry[2].get('link') for entry in confirmed}
            for item in reconciled.values():
                info=_evidence(item['job_id'],db)
                if info.get('link') not in existing:
                    confirmed.append((f"{info.get('employer')} - {info.get('title')} [Applied; read-only reconciliation]",item['confirmation_path'],info))
        lines.append(f"Applications confirmed: {len(confirmed)}")
        for label,proof,info in confirmed:
            lines.append(f"  {label}\n    proof: {proof}\n    listing: {info.get('link')}")
        for b in result.get('blockers',[]):lines.append(f"Blocker: source {b['source']} - {b['reason']}")
        for label,item,info in blockers:
            note=item.get('question') or item.get('error_message') or item.get('blocker') or item.get('error_type') or ''
            if item.get('incomplete'):note+=f" (safely incomplete after {item.get('answered',0)} saved answer(s); continues automatically once the answer is confirmed)"
            lines.append(f"Blocker: {label} {note}\n    link: {info.get('link')}\n    evidence: {', '.join(info.get('evidence',[]))}")
            if item.get('generic_resume_restored') is False:lines.append('  WARNING: generic resume restore not verified; check the Naukri profile')
        for e in result.get('errors',[]):lines.append(f'Error: {e}')
        alerts=result.get('blocker_alerts',[])
        sent=sum(bool(a.get('desktop_sent')) for a in alerts)
        lines.append(f'Alerts: {sent} sent now, {len(alerts)-sent} suppressed (cooldown/acknowledged)')
        lines.append('Run evidence: '+', '.join(result.get('evidence_paths',[]))+'; views/quick.html')
    finally:db.close()
    return '\n'.join(lines),True
