"""Concise status banner for views/quick.html (read-only; canonical data stays in SQLite)."""
import datetime as dt
import html
import json
from pathlib import Path

import job_db

START='<!--status-->'
END='<!--/status-->'

def _esc(value):return html.escape(str(value if value is not None else ''))

def _link(url):
    return '<a href="'+_esc(url)+'">open</a>' if str(url or '').startswith('https://') else ''

def _outcome(root,slug,name):
    path=Path(root)/'jobs'/str(slug)/name
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return {}

def source_label(info):
    status=info.get('status')
    if status=='Success':return 'OK'
    if status in ('Blocked','Failed','BlankPage','CardsWithoutExactLinks'):return 'SOURCE FAILURE'
    if status=='NoVisibleCards':return 'NO CARDS (source problem, not zero jobs)'
    return status or 'unknown'

def collect(db,root,config=None):
    rows={r['state']:r['n'] for r in db.execute('SELECT state,count(*) n FROM jobs GROUP BY state')}
    def listing(sql,*args):return [dict(r) for r in db.execute(sql,args)]
    applied=listing("SELECT id,employer,title,requisition_id,canonical_url,slug,status_date FROM jobs WHERE state='Applied' ORDER BY status_date DESC")
    needs=listing("SELECT id,employer,title,canonical_url,slug,status_date FROM jobs WHERE state='NeedsAnswer' ORDER BY status_date DESC")
    uncertain=listing("SELECT id,employer,title,canonical_url,slug,status_date FROM jobs WHERE state IN ('SubmissionUnconfirmed','NeedsReview') ORDER BY status_date DESC")
    urgent=listing("""SELECT id,employer,title,canonical_url,state,status_date FROM jobs
        WHERE state IN ('Interview','Offer') OR (lower(tracker_json) LIKE '%urgent%' AND state NOT IN ('SubmissionUnconfirmed','NeedsReview')) ORDER BY status_date DESC""")
    queue={r['state']:r['n'] for r in db.execute('SELECT state,count(*) n FROM queue GROUP BY state')}
    mail=None
    try:
        report=json.loads((Path(root)/'.workflow/reconcile/latest.json').read_text())
        gaps=json.loads((Path(root)/'.workflow/reconcile/history-gaps.json').read_text())
        mail={'generated_at':report['generated_at'],'total':report['total'],'confidence_counts':report['confidence_counts'],
              'urgent':[{'kind':i['kind'],'subject':i['record'].get('subject'),'employer':i.get('employer_hint'),'confidence':i['confidence']} for i in report['urgent']],
              'history_gaps':len(gaps),'history_gaps_blocking':sum(g.get('scope') in ('requisition','role') for g in gaps)}
    except (OSError,ValueError,KeyError):pass
    ready=listing("""SELECT j.employer,j.title,j.fit,q.state FROM queue q JOIN jobs j ON j.id=q.job_id
        WHERE q.state IN ('QueuedResume','ResumeReady','ResumeFailed') AND j.state IN ('Shortlisted','ResumeReady','Prepared') ORDER BY (j.fit='High') DESC,q.priority DESC LIMIT 8""")
    near=[]
    try:
        near=listing("""SELECT j.employer,j.title,l.reasons_json FROM auto_fit_log l JOIN jobs j ON j.id=l.job_id
            WHERE l.outcome IN ('not_eligible','blocked_duplicate') AND j.state='Verified' AND l.fit='Medium'
            ORDER BY l.at DESC LIMIT 5""")
    except Exception:pass
    run=db.execute("SELECT id,started_at,finished_at,status,result_json FROM runs WHERE status!='Running' ORDER BY started_at DESC").fetchall()
    real=next((r for r in run if not json.loads(r['result_json'] or '{}').get('test')),None)
    last=json.loads(real['result_json']) if real else {}
    for item in applied:
        audit=_outcome(root,item['slug'],'application-outcome-audit.json')
        item['outcome_label']='Applied on listing — reconciliation pending' if (
            audit.get('retry_allowed') is False and audit.get('listing_applied') is True and
            (audit.get('independently_reconciled') is False or audit.get('workflow_worker_proven') is False)) else 'Applied'
        if audit.get('history_reconciled') is True:
            item['outcome_label']='Applied — history confirmed; email reconciliation pending' if audit.get('independently_reconciled') is False else 'Applied — history confirmed'
        if audit.get('candidate_facts_reconciled') is False:item['outcome_label']=audit.get('outcome_label') or 'Applied — candidate facts need review'
        item['proof']=bool(_outcome(root,item['slug'],'naukri-native-confirmation.json') or
                           _outcome(root,item['slug'],'naukri-history-reconciliation-confirmation.json') or
                           _outcome(root,item['slug'],'linkedin-history-reconciliation-confirmation.json') or
                           _outcome(root,item['slug'],'confirmation.json') or
                           _outcome(root,item['slug'],'naukri-observed-application-proof.json'))
    for item in uncertain:
        outcome=_outcome(root,item['slug'],'naukri-native-outcome.json')
        item['pending_question']=outcome.get('pending_question') or _outcome(root,item['slug'],'naukri-native-observation.json').get('pending_question')
    for item in needs:
        outcome=_outcome(root,item['slug'],'linkedin-easy-apply-outcome.json') or _outcome(root,item['slug'],'naukri-native-outcome.json')
        item['reason']=outcome.get('reason') or outcome.get('blocker') or 'see job folder'
        item['question']=outcome.get('question')
    providers={}
    if config:
        import job_config
        from portals import registry
        caps=registry.capabilities(db,config,persist=False)
        providers={p:{'manual':job_config.live_provider_enabled(config,p) and caps[p]['capability']=='ApplyReady',
            'scheduled':job_config.live_provider_enabled(config,p,timer=True) and caps[p]['capability']=='ApplyReady',
            'max':config['portal_workers'][p].get('max_submissions_per_run',1),'reason':caps[p]['reason']} for p in ('naukri','linkedin')}
    return {'generated_at':dt.datetime.now(dt.timezone.utc).isoformat(),'counts':rows,'applied':applied,'needs_answer':needs,
            'uncertain':uncertain,'urgent':urgent,'mail':mail,'queue':queue,'ready':ready,'near_misses':near,
            'last_run':{'id':real['id'] if real else None,'finished_at':real['finished_at'] if real else None,
                        'status':real['status'] if real else None,'scheduled':last.get('scheduled'),
                        'sources':{k:{**v,'label':source_label(v)} for k,v in (last.get('sources') or {}).items()},
                        'stage_counts':last.get('stage_counts',{}),'auto_fit':last.get('auto_fit'),
                        'naukri_worker':last.get('naukri_application_worker')},
            'lane':{'naukri_worker_enabled':bool(config and config['naukri_worker']['enabled']),
                    'global_submissions_enabled':bool(config and config['submissions']['enabled']),'providers':providers}}

def render(status):
    def table(rows,columns,empty):
        if not rows:return '<p class="none">'+_esc(empty)+'</p>'
        return '<table><tr>'+''.join('<th>'+_esc(c)+'</th>' for c in columns)+'</tr>'+''.join(
            '<tr>'+''.join('<td>'+cell+'</td>' for cell in row)+'</tr>' for row in rows)+'</table>'
    lane=status['lane'];last=status['last_run'];q=status['queue']
    provider_text=' · '.join(p+': Run Now '+('live' if v['manual'] else 'discovery only')+', scheduled '+('live' if v['scheduled'] else 'off')+', max '+str(v['max'])+' new attempts/run' for p,v in lane.get('providers',{}).items())
    head=('<h2>Status</h2><p><b>Naukri auto-apply / LinkedIn Easy Apply:</b> '+provider_text
          +' · other portals disabled · global submissions '+('ON' if lane['global_submissions_enabled'] else 'off')
          +'<br><b>Applied</b> '+str(len(status['applied']))+' · <b>Needs answer</b> '+str(len(status['needs_answer']))
          +' · <b>Uncertain</b> '+str(len(status['uncertain']))+' · <b>Urgent</b> '+str(len(status['urgent']))
          +' · queue: '+_esc(', '.join(f'{k} {v}' for k,v in sorted(q.items())) or 'empty')+'</p>')
    blocks=[head,
      '<h3>Urgent responses</h3>'+table([[_esc(r['employer']),_esc(r['title']),_esc(r['state']),_link(r['canonical_url'])] for r in status['urgent']],['Company','Role','State','Link'],'None'),
      '<h3>Needs answer / handoff</h3>'+table([[_esc(r['employer']),_esc(r['title']),_esc(r['question'] or r['reason']),_link(r['canonical_url'])] for r in status['needs_answer']],['Company','Role','Needed','Link'],'None'),
      '<h3>Uncertain (locked, never retried)</h3>'+table([[_esc(r['employer']),_esc(r['title']),_esc(str(r['status_date'])[:10]),_esc(r.get('pending_question') or ''),_link(r['canonical_url'])] for r in status['uncertain']],['Company','Role','Since','Open question','Check on Naukri'],'None'),
      '<h3>Applied</h3>'+table([[_esc(r['employer']),_esc(r['title']),_esc(str(r['status_date'])[:10]),_esc(r.get('outcome_label','Applied')),'proof saved' if r['proof'] else 'no proof file'] for r in status['applied']],['Company','Role','Date','Outcome','Evidence'],'None'),
      '<h3>Next in line for the Naukri worker</h3>'+table([[_esc(r['employer']),_esc(r['title']),_esc(r['fit']),_esc(r['state'])] for r in status['ready']],['Company','Role','Fit','Stage'],'Nothing staged'),
    ]
    mail=status.get('mail')
    if mail:
        blocks.append('<h3>Mailbox reconciliation (read-only) · '+_esc(str(mail['generated_at'])[:16])+' UTC</h3><p>'+_esc(mail['total'])+' messages · '
                      +_esc(', '.join(f'{k} {v}' for k,v in sorted(mail['confidence_counts'].items())))+' · history gaps (applied elsewhere, not tracked): '+_esc(mail['history_gaps'])+' ('+_esc(mail.get('history_gaps_blocking',0))+' can block a matching role; employer-only ones never block)'+'</p>'
                      +table([[_esc(u['kind']),_esc(u['employer']),_esc(u['subject']),_esc(u['confidence'])] for u in mail['urgent']],['Kind','Employer','Subject','Match'],'No interview/offer messages found'))
    else:
        blocks.append('<h3>Mailbox reconciliation</h3><p class="none">Not run. Gmail is not read automatically; capture message metadata and run <code>python3 workflow reconcile &lt;capture.json&gt;</code>.</p>')
    sources=last['sources']
    if sources:
        blocks.append('<h3>Last full run coverage · '+_esc(str(last['finished_at'])[:16])+' UTC · '+_esc(last['status'])+'</h3>'+
            table([[_esc(k),_esc(v['label']),_esc(v.get('leads')),_esc(v.get('unique_originals_verified',v.get('feed_jobs'))),_esc(v.get('error_kind') or '')] for k,v in sorted(sources.items())],
                  ['Source','Result','Leads','Verified','Error'],'No run yet'))
    sc=last['stage_counts']
    if sc:
        blocks.append('<p>Funnel: fetched '+_esc(sc.get('fetched'))+' → fresh '+_esc(sc.get('fresh'))+' / unknown date '+_esc(sc.get('unknown_date'))
                      +' → reviewed '+_esc(sc.get('original_or_portal_jds_reviewed'))+' → queued for resume '+_esc(sc.get('queued_for_resume'))+'</p>')
    if last.get('auto_fit'):
        af=last['auto_fit'];blocks.append('<p>Auto fit review: considered '+_esc(af.get('considered'))+', queued '+_esc(af.get('queued'))+', not eligible '+_esc(af.get('not_eligible'))+', blocked '+_esc(af.get('blocked'))+'</p>')
    if status['near_misses']:
        blocks.append('<h3>Near-misses (Medium, not queued)</h3>'+table([[_esc(r['employer']),_esc(r['title']),_esc('; '.join(json.loads(r['reasons_json'])[:2]))] for r in status['near_misses']],['Company','Role','Why not'],''))
    return START+'<div class="status">'+''.join(blocks)+'</div>'+END

def write(root,config=None,database=None):
    root=Path(root);db=job_db.connect(database or root/'.workflow/jobs.sqlite3')
    try:status=collect(db,root,config)
    finally:db.close()
    (root/'views/status.json').write_text(json.dumps(status,indent=2,default=str)+'\n')
    quick=root/'views/quick.html'
    if quick.is_file():
        text=quick.read_text();banner=render(status)
        if START in text:text=text[:text.index(START)]+banner+text[text.index(END)+len(END):]
        else:text=text.replace('<h1>Quick tracker</h1>','<h1>Quick tracker</h1>'+banner,1)
        text=text.replace('<style>','<style>.status{background:#f6f8fa;padding:12px;margin-bottom:16px}.none{color:#666}',1) if '.status{' not in text else text
        quick.write_text(text)
    return {'status_json':'views/status.json','applied':len(status['applied']),'needs_answer':len(status['needs_answer']),
            'uncertain':len(status['uncertain'])}
