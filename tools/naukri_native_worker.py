"""Reusable, one-per-run Naukri native-Apply worker with fail-closed evidence gates."""
from __future__ import annotations
import contextlib
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from local_paths import ROOT
import job_db
from application_budget import AttemptBudget,claim_id,pace,preflight_failure
from naukri_phase_guard import NaukriPhaseGuard, APPLY_PATH, CHAT_PATH
from naukri_direct_playwright import _chrome, _status
from infosys_resume_flow import current_filename, upload_profile

GENERIC=ROOT/'master-resume/Yugal_Teotia_CV.pdf'
MAX_QUESTIONS=20
MAX_JOBS_TRIED_PER_RUN=30
RETRY_AFTER_BLOCK_HOURS=6
MAX_BLOCKED_TRIES=3
MAX_RESUMES=MAX_QUESTIONS

def _now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def _sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def strong_medium_allowed(job,config):
    saved=json.loads(job['fit_json'] or '{}')
    from fit_engine import evaluate
    decision=saved.get('decision') or {}
    verdict=evaluate(dict(job),decision,config,ROOT/'master-resume/Yugal_Teotia_CV.tex')
    return bool(job['fit']=='Medium' and verdict.get('strong_medium_eligible') is True
                and saved.get('result',{}).get('strong_medium_eligible') is True)

def validate_binding(job,resume_path,visual_review,answer_profile,config,resume=False,checked_continuation=False):
    """Pure input gate shared by live and synthetic worker tests."""
    if str(job['requisition_id']) in {str(x) for x in config['naukri_worker']['excluded_requisition_ids']}:
        raise ValueError('Permanently excluded requisition')
    if job['source']!='naukri_portal_native' or not str(job['canonical_url']).startswith('https://www.naukri.com/job-listings-'):
        raise ValueError('Exact Naukri native-Apply listing required')
    allowed=('SubmissionUnconfirmed',) if checked_continuation else ('Prepared','ResumeReady','NeedsAnswer') if resume else ('Prepared','ResumeReady')
    if job['state'] not in allowed or job['availability']!='Live':
        raise ValueError('Job must be live and staged with a verified JD')
    parsed=urlsplit(job['canonical_url'])
    if parsed.hostname!='www.naukri.com' or not parsed.path.rstrip('/').endswith(str(job['requisition_id'])):
        raise ValueError('Canonical URL does not bind to the requisition ID')
    if job['fit']=='High':
        saved=json.loads(job['fit_json'] or '{}')
        from fit_engine import evaluate
        if not saved.get('decision') or not evaluate(dict(job),saved['decision'],config,ROOT/'master-resume/Yugal_Teotia_CV.tex').get('eligible'):
            raise ValueError('High-fit decision/evidence is absent or stale')
    elif not strong_medium_allowed(job,config):
        raise ValueError('Medium role does not pass the approved strong-Medium evidence rule')
    path=(ROOT/resume_path).resolve()
    folder=(ROOT/'jobs'/job['slug']).resolve()
    if not path.is_relative_to(folder) or not path.is_file() or path.suffix.lower()!='.pdf':
        raise ValueError('Role PDF must be inside this job folder')
    digest=_sha(path)
    if visual_review.get('status')!='visually_clean' or visual_review.get('pdf_sha256')!=digest:
        raise ValueError('Exact role PDF lacks current visual approval')
    if not answer_profile or 'CONFIRMED' not in answer_profile:
        raise ValueError('Confirmed answer profile is unavailable')
    return {'job_id':job['id'],'requisition_id':str(job['requisition_id']),'url':job['canonical_url'],
            'resume_path':str(path.relative_to(ROOT)),'resume_sha256':digest,'resume_filename':path.name,
            'fit':job['fit'],'fit_json_sha256':hashlib.sha256(job['fit_json'].encode()).hexdigest()}

def candidates(db,config,limit=20):
    rows=db.execute("""SELECT DISTINCT j.* FROM jobs j JOIN queue q ON q.job_id=j.id
        WHERE j.source='naukri_portal_native' AND j.state IN ('Prepared','ResumeReady')
        AND j.availability='Live' AND j.fit IN ('High','Medium')
        AND q.state IN ('ResumeReady','QueuedResume')
        AND (q.retry_after IS NULL OR q.retry_after<=?)
        AND NOT EXISTS(SELECT 1 FROM submission_attempts a WHERE a.job_id=j.id)
        AND NOT EXISTS(SELECT 1 FROM history_keys h WHERE h.employer_key=j.employer_key AND h.requisition_key=j.requisition_key)
        ORDER BY (j.fit='High') DESC,q.priority DESC,q.queued_at,j.id LIMIT ?""",(_now(),limit)).fetchall()
    excluded={str(x) for x in config['naukri_worker']['excluded_requisition_ids']}
    import response_reconcile
    return [row for row in rows if str(row['requisition_id']) not in excluded
            and not response_reconcile.history_gap_blocks(row['employer_key'],row['title'],requisition_id=row['requisition_id'])]

def _marker():return ROOT/'.workflow/profile-restore-pending.json'

def _write_marker(job_id,req):
    _marker().write_text(json.dumps({'job_id':job_id,'requisition_id':str(req),'at':_now(),'reason':'role resume may be the active Naukri profile resume'})+'\n')

def ensure_generic_resume(page,job,req,evidence_path):
    """Wait for resume metadata, repair when needed, and retain explicit proof."""
    expected=_sha(GENERIC);samples=[];observed=None;previous=None
    for _ in range(10):
        observed=current_filename(page);samples.append(observed)
        if observed and observed==previous:break
        previous=observed;page.wait_for_timeout(500)
    receipt_path=ROOT/'.workflow/naukri-live-verified.json'
    try:prior=json.loads(receipt_path.read_text())
    except (OSError,ValueError):prior={}
    hash_known=prior.get('generic_resume_restored') is True and prior.get('generic_resume_sha256')==expected
    repair=observed!=GENERIC.name or not hash_known
    evidence={'job_id':job['id'],'requisition_id':str(req),'expected_filename':GENERIC.name,
              'expected_sha256':expected,'initial_filename':samples[0],'observed_filename':observed,
              'filename_samples':samples,'previous_verified_sha256':prior.get('generic_resume_sha256'),
              'repair_performed':repair,'verified':False,'before_application_action':True,'at':_now()}
    try:
        if repair:
            _write_marker(job['id'],req)
            uploaded=upload_profile(page,GENERIC)
            evidence['upload']=uploaded
            if (uploaded.get('after')!=GENERIC.name or uploaded.get('pdf_sha256')!=expected
                    or not any(200<=s.get('status',0)<300 for s in uploaded.get('response_statuses',[]))):
                raise RuntimeError('Generic resume upload lacks filename, hash or server acknowledgement')
        evidence['final_filename']=current_filename(page)
        if evidence['final_filename']!=GENERIC.name:raise RuntimeError('Generic resume filename did not verify after repair')
        evidence.update(verified=True,verified_at=_now(),verification_basis='uploaded_expected_bytes_and_server_acknowledgement' if repair else 'stable_profile_filename_and_previous_verified_upload_hash')
        if repair:_marker().unlink(missing_ok=True)
    except Exception as exc:
        evidence['error_type']=type(exc).__name__
        raise RuntimeError('Generic resume repair could not be verified; inspect the saved startup restoration evidence') from exc
    finally:evidence_path.write_text(json.dumps(evidence,indent=2)+'\n')
    return evidence

def recover_pending_restore():
    """Crash recovery: if a role resume may still be active, restore and verify the generic one first."""
    if not _marker().is_file():return {'status':'None'}
    try:
        with _chrome(headless=False) as context:
            page=context.pages[0] if context.pages else context.new_page()
            if not _status(page)['signed_in']:raise RuntimeError('Isolated Naukri profile is signed out')
            if current_filename(page)!=GENERIC.name:upload_profile(page,GENERIC)
            ok=current_filename(page)==GENERIC.name
        if ok:
            _marker().unlink(missing_ok=True);return {'status':'Restored'}
        return {'status':'Failed','error_type':'GenericNotActive'}
    except Exception as exc:
        return {'status':'Failed','error_type':type(exc).__name__}

@contextlib.contextmanager
def _browser_session(state):
    """Chrome context whose exit restores the generic profile resume while the page is still alive."""
    with _chrome(headless=False) as context:
        try:
            yield context
        finally:
            page=state.get('page')
            if state.get('restore_needed') and page is not None:
                try:
                    # The application network guard must not block the profile upload used for restoration.
                    context.unroute_all(behavior='ignore_errors')
                    restored=upload_profile(page,GENERIC)
                    state['restore_sha256']=restored.get('pdf_sha256')
                    state['restored']=restored['after']==GENERIC.name and current_filename(page)==GENERIC.name
                    if state['restored']:_marker().unlink(missing_ok=True)
                except Exception:
                    state['restored']=False

def _listing(page,url,req):
    page.goto(url,wait_until='domcontentloaded',timeout=30000);page.wait_for_timeout(1100)
    return page.evaluate(LISTING_STATE_SCRIPT,str(req))

LISTING_STATE_SCRIPT=r'''req=>{const visible=x=>!!x.getClientRects().length;
      const b=[...document.querySelectorAll('#apply-button')].filter(visible);
      const a=[...document.querySelectorAll('#already-applied')].filter(x=>visible(x)&&x.closest('[class*="jhc__apply-button-container"]'));
      const id=(location.pathname.match(/\/job-listings-.*-(\d{8,})\/?$/)||[])[1];
      const exact=location.hostname==='www.naukri.com'&&id===req;
      const containers=[...document.querySelectorAll('[class*="jhc__apply-button-container"]')].filter(visible);
      const t=document.body.innerText;return {exact_path:exact,apply_count:b.filter(x=>x.innerText.trim()==='Apply').length,
      applied:exact&&a.length===1&&a[0].innerText.trim()==='Applied'&&b.length===0,
      applied_control_count:a.length,applied_control_labels:a.map(x=>x.innerText.trim()),
      pending:containers.some(x=>/Continue application|Pending application/i.test(x.innerText)),
      site_restriction:/captcha|verify you are human|account (?:restricted|suspended)|daily application limit|reached.*application limit|too many requests/i.test(t)}}'''

def _chat(page):
    return page.evaluate('''()=>{const d=[...document.querySelectorAll('.chatbot_Drawer')].find(x=>x.getClientRects().length);const r=d||document;
      const v=x=>!!x&&!!x.getClientRects().length;const prompts=[...r.querySelectorAll('.botMsg')].filter(v).map(x=>x.innerText.trim()).filter(x=>!/^hi yugal/i.test(x));
      const edit=[...r.querySelectorAll('[contenteditable=true]')].filter(v);const textInputs=[...r.querySelectorAll('input[type=text],input[type=number],textarea')].filter(x=>v(x)&&!x.disabled);
      const selects=[...r.querySelectorAll('select')].filter(x=>v(x)&&!x.disabled).map(x=>({id:x.id,options:[...x.options].map(o=>({text:o.text,value:o.value}))}));
      const saves=[...r.querySelectorAll('.sendMsgbtn_container')].filter(v);
      const options=[...r.querySelectorAll('label[for]')].filter(v).map(x=>{const id=x.getAttribute('for')||'';const input=id?r.querySelector('[id="'+id.replace(/"/g,'')+'"]'):null;
        return {text:x.innerText.trim(),id,type:input?.type,value:input?.value,checked:!!input&&!!input.checked}}).filter(x=>['radio','checkbox'].includes(x.type));
      return {drawer:!!d,prompts,options,selects,text_input_count:textInputs.length,editor_count:edit.length+textInputs.length,editor_empty:[...edit.map(x=>!x.innerText.trim()),...textInputs.map(x=>!x.value.trim())],save_count:saves.length,
      save_label:saves[0]?.innerText.trim()||'',save_disabled:!!saves[0]?.classList.contains('disabled'),thank_you:/Thank you for your responses/i.test(d?.innerText||''),
      site_restriction:/captcha|verify you are human|account (?:restricted|suspended)|daily application limit|reached.*application limit|too many requests/i.test(document.body.innerText)};}''')

def _question_key(ui,phase):
    node=next((e.get('response_metadata',{}).get('currentNodeName') for e in reversed(phase.events)
               if e.get('response_metadata',{}).get('currentNodeName')),None)
    question=ui['prompts'][-1] if ui.get('prompts') else ''
    return hashlib.sha256(json.dumps([question,node,[(o.get('text'),o.get('type')) for o in ui.get('options',[])],
                                     ui.get('selects',[])],sort_keys=True).encode()).hexdigest()

def _payload_matches(given,expected):
    def values(x):return sorted(str(v).strip().casefold() for v in (x if isinstance(x,list) else [x]))
    return isinstance(given,(str,list)) and values(given)==values(expected)

def _stage_answer(page,ui,answer):
    """Fill one exact current text/radio/checkbox/dropdown control and verify it."""
    values=answer if isinstance(answer,list) else [str(answer)]
    selects=ui.get('selects') or []
    if len(selects)==1:
        matches=[o for o in selects[0]['options'] if o['text'].strip().casefold()==str(answer).strip().casefold()]
        if len(matches)!=1:return False,[]
        select=page.locator('.chatbot_Drawer select:visible');select.select_option(value=matches[0]['value'])
        return select.input_value()==matches[0]['value'],[str(answer),matches[0]['value']]
    if ui['editor_count']==0 and ui.get('options'):
        matches=[o for o in ui['options'] if o['text'].strip().casefold() in {str(v).strip().casefold() for v in values}]
        if len(matches)!=len(values) or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',o['id']) for o in matches):return False,[]
        checkbox=all(o.get('type')=='checkbox' for o in ui['options'])
        if checkbox:
            wanted={o['id'] for o in matches}
            for o in ui['options']:
                page.locator('.chatbot_Drawer [id="'+o['id']+'"]').set_checked(o['id'] in wanted)
        else:
            if len(matches)!=1 or any(o['checked'] and o['id']!=matches[0]['id'] for o in ui['options']):return False,[]
            page.locator('.chatbot_Drawer label[for="'+matches[0]['id']+'"]').click(timeout=5000)
        if not all(page.locator('.chatbot_Drawer [id="'+o['id']+'"]').is_checked() for o in matches):return False,[]
        labels=[o['text'] for o in matches];native=[o.get('value') or o['text'] for o in matches]
        return True,[labels if checkbox else labels[0],native if checkbox else native[0]]
    if ui['editor_count']==1:
        if ui.get('text_input_count'):
            editor=page.locator('.chatbot_Drawer input[type=text]:visible,.chatbot_Drawer input[type=number]:visible,.chatbot_Drawer textarea:visible')
            editor.fill(str(answer),timeout=5000);verified=editor.input_value().strip()==str(answer).strip()
        else:
            editor=page.locator('.chatbot_Drawer [contenteditable=true]');editor.fill(str(answer),timeout=5000);verified=editor.inner_text().strip()==str(answer).strip()
        return verified,[str(answer)]
    return False,[]

def _progress_path(folder):return Path(folder)/'naukri-native-progress.json'

def _load_progress(folder):
    try:return json.loads(_progress_path(folder).read_text())
    except (OSError,ValueError):return None

def _save_progress(folder,attempt_id,req,answered,pending_question,*,status,resumes=0,save_in_flight=False):
    """Durable questionnaire progress: question text and fact references only, never answer values."""
    _progress_path(folder).write_text(json.dumps({'attempt_id':attempt_id,'requisition_id':str(req),'status':status,
        'answered':answered,'saves':len(answered),'pending_question':pending_question,'resumes':resumes,
        'save_in_flight':save_in_flight,'updated_at':_now(),'answers_logged':False},indent=2,ensure_ascii=False)+'\n')

def _verify_incomplete(page,phase,url,req,job,folder,attempt_id,question):
    """Read-only check that a claimed questionnaire stopped before completion. Any doubt => not safe (stays Uncertain)."""
    summary=phase.summary()
    denied=sum(1 for e in summary['events'] if e.get('allowed') is False)
    listing=_listing(page,url,req);history=_history(page,req,job['employer'],job['title'])
    evidence={'attempt_id':attempt_id,'requisition_id':str(req),'checked_at':_now(),'pending_question':question,
              'completion_count':summary['completion_count'],'workflow_committed':summary['workflow_committed'],
              'denied_events':denied,'save_in_flight':False,'listing_exact_path':bool(listing['exact_path']),
              'listing_applied':bool(listing['applied']),'history_full':bool(history['full']),'history_exact':history['exact'],
              'history_company_role':history['company_role'],'answers_logged':False}
    evidence['safe']=bool(listing['exact_path'] and not listing['applied'] and history['full'] and history['exact']==0
                          and summary['completion_count']==0 and not summary['workflow_committed'] and denied==0)
    path=Path(folder)/'naukri-native-incomplete-evidence.json'
    path.write_text(json.dumps(evidence,indent=2,ensure_ascii=False)+'\n')
    evidence['evidence_path']=str(path.relative_to(ROOT))
    return evidence

def resumable(db,config,profile):
    """Safely incomplete attempts whose pending question is now answerable from confirmed facts."""
    import response_reconcile
    excluded={str(x) for x in config['naukri_worker']['excluded_requisition_ids']}
    found=[]
    for row in db.execute("""SELECT j.*,a.attempt_id AS claimed_attempt FROM jobs j JOIN submission_attempts a ON a.job_id=j.id
        WHERE j.source='naukri_portal_native' AND j.state='NeedsAnswer' AND a.state='incomplete' AND j.availability='Live'
        ORDER BY j.status_date""").fetchall():
        progress=_load_progress(ROOT/'jobs'/row['slug'])
        if (not progress or progress.get('attempt_id')!=row['claimed_attempt'] or progress.get('status')!='incomplete'
                or progress.get('resumes',0)>=MAX_RESUMES or str(row['requisition_id']) in excluded):continue
        question=progress.get('pending_question')
        if not question or _answer(question,profile)[0] is None:continue
        if db.execute('SELECT 1 FROM history_keys h WHERE h.employer_key=? AND h.requisition_key=?',(row['employer_key'],row['requisition_key'])).fetchone():continue
        if response_reconcile.history_gap_blocks(row['employer_key'],row['title'],requisition_id=row['requisition_id']):continue
        found.append((row,progress))
    return found

def _reopen(page,phase,url,req):
    """Continue the SAME claimed attempt when Naukri stops the chat after a Save (no new claim, no answer replay).

    Only one guarded `show` request is allowed by the phase guard; a live, unapplied listing is required first.
    """
    import control,job_config
    if control.is_paused() or not job_config.live_naukri_enabled(job_config.load()):return False
    if not phase.may_reopen():return False
    listing=_listing(page,url,req)
    if listing['applied'] or not listing['exact_path'] or listing['apply_count']!=1 or listing['pending']:return False
    phase.arm_reopen()
    page.locator('#apply-button:visible').filter(has_text='Apply').click(timeout=10000);page.wait_for_timeout(3500)
    ui=_chat(page)
    return phase.phase=='followup_status_received' and bool(ui['drawer']) and (bool(ui['prompts']) or bool(ui['thank_you']))

def _answer(question,profile):
    """Only low-ambiguity facts with direct confirmation in the profile are reusable (rules: config/candidate.yaml)."""
    import candidate
    return candidate.answer(question,profile)

def _job_bound_commit(phase,req,listing=None,history=None):
    """Require a claimed, job-bound Save plus positive application evidence.

    A ``show`` response with ``dataCommitted=false`` is only a status refresh.
    Naukri can nevertheless complete its native chat without a distinct
    completion request, so an exact Applied listing or fully loaded Applied
    Jobs history is accepted when the claimed chat answer succeeded for this
    requisition.
    """
    for event in phase.events:
        if event.get('job_id')!=str(req) or not isinstance(event.get('response_status'),int) or not 200<=event['response_status']<300:
            continue
        meta=event.get('response_metadata',{})
        if event.get('reason')=='claimed_save_completion' and meta.get('job_id_match') is True:
            return event
        if (event.get('reason') in ('claimed_job_status_refresh','post_completion_job_status')
                and meta.get('job_id_match') is True and meta.get('dataCommitted') is True):
            return event
    applied=(bool(listing and listing.get('exact_path') and listing.get('applied')) or
             bool(history and history.get('full') and history.get('exact',0)>0))
    saved_answer=any(event.get('job_id')==str(req) and
        event.get('reason')=='one_claimed_chat_answer' and
        isinstance(event.get('response_status'),int) and 200<=event['response_status']<300 and
        event.get('response_metadata',{}).get('job_id_match') is True for event in phase.events)
    if applied and saved_answer and phase.save_arm_count==phase.answer_count and phase.save_arm_count>0:
        return {'job_id':str(req),'reason':'positive_job_bound_applied_ui_confirmation',
                'response_status':200,'flowtype':'unknown','response_metadata':{'job_id_match':True},
                'evidence':'exact_listing_or_full_history'}
    if applied and phase.claimed_attempt_id and any(e.get('reason')=='exact_job_show' and
            e.get('job_id')==str(req) and isinstance(e.get('response_status'),int) and
            200<=e['response_status']<300 for e in phase.events):
        return {'job_id':str(req),'reason':'positive_exact_applied_after_claimed_open','response_status':200,
                'flowtype':'show','response_metadata':{'job_id_match':True},'evidence':'exact_listing_or_full_history'}
    return None

def _history(page,req,company,title,exact_ids=None):
    server_rows=[];server_company_role={}
    observed_ids={str(x) for x in (exact_ids or [req])}
    def observe(response):
        parsed=urlsplit(response.url);path=parsed.path
        if parsed.hostname!='www.naukri.com' or response.request.method!='GET' or response.status!=200 or path!='/cloudgateway-apply/whtma-services/v0/applyapi/v5/history':return
        try:data=response.json()
        except Exception:return
        def walk(value):
            if isinstance(value,dict):
                if str(value.get('jobId')) in observed_ids and 'jobTitle' in value and 'company' in value:
                    server_rows.append({'job_id':str(value['jobId']),'response_path':path,
                        'title':value['jobTitle'],'company':value['company'],'status':value.get('status')})
                identifier=str(value.get('jobId') or '')
                if (re.fullmatch(r'\d{8,}',identifier)
                        and company.casefold() in str(value.get('company') or '').casefold()
                        and title.casefold() in str(value.get('jobTitle') or '').casefold()):
                    server_company_role[identifier]={'job_id':identifier,'title':value['jobTitle'],'company':value['company'],'response_path':path}
                for child in value.values():walk(child)
            elif isinstance(value,list):
                for child in value:walk(child)
        walk(data)
    if hasattr(page,'on'):page.on('response',observe)
    page.goto('https://www.naukri.com/myapply/historypage',wait_until='domcontentloaded',timeout=30000);page.wait_for_timeout(1000)
    source=page.locator('.apply-filters li').filter(has_text='Applies on Naukri')
    if source.count()!=1:raise RuntimeError('Applied Jobs filter unavailable')
    source.click();page.wait_for_timeout(1500);sweeps=[];previous=-1;stagnant=0
    for _ in range(25):
        snap=page.evaluate('''x=>{const rows=[...document.querySelectorAll('.jdTuplesContainer .jdTupleContainer')];
          const label=[...document.querySelectorAll('.apply-filters li')].map(e=>e.innerText.trim()).find(v=>v.includes('Applies on Naukri'))||'';
          const m=label.match(/\\((\\d+)\\)/);const more=document.querySelector('#loadMoreLnk');
          const exactPattern=new RegExp('(?:^|[^0-9])'+x.req+'(?:[^0-9]|$)');
          const exactRow=r=>[...r.querySelectorAll('a[href]')].some(a=>{
            const m=new URL(a.href,location.href).pathname.match(/job-listings-.*-(\\d{8,})\\/?$/);
            return m&&m[1]===x.req;});
          const exact=rows.filter(exactRow);
          const ambiguous=rows.filter(r=>!exactRow(r)&&
            ![...r.querySelectorAll('a[href]')].some(a=>/job-listings-.*-\\d{8,}/.test(a.href))&&
            (r.innerText||'').toLowerCase().includes(x.company.toLowerCase())&&(r.innerText||'').toLowerCase().includes(x.title.toLowerCase()));
          return {rows:rows.length,total:m?Number(m[1]):null,exact:exact.length,company_role:ambiguous.length,more:!!more&&!!more.getClientRects().length}}''',{'req':str(req),'company':company,'title':title})
        sweeps.append(snap)
        if (snap['total'] is not None and snap['rows']>=snap['total']) or not snap['more']:break
        stagnant=stagnant+1 if snap['rows']<=previous else 0
        if stagnant>=3:break
        previous=snap['rows']
        # The list scrolls inside its own 800px container; the 1px sentinel is only clickable once that container is at its end.
        page.evaluate("()=>{const c=document.querySelector('.jdTuplesContainer');if(c)c.scrollTop=c.scrollHeight}");page.wait_for_timeout(500)
        # Scrolling can itself load the final page and hide the sentinel.
        # Recheck before clicking so a completed history is not misreported as a browser failure.
        count=page.locator('.jdTuplesContainer .jdTupleContainer').count()
        if snap['total'] is not None and count>=snap['total']:continue
        more=page.locator('#loadMoreLnk')
        if not more.is_visible():continue
        try:more.click(timeout=5000,force=True)
        except Exception:
            if more.is_visible():raise
        page.wait_for_timeout(1800)
    end=sweeps[-1]
    full=end['total'] is not None and end['rows']>=end['total']
    if hasattr(page,'remove_listener'):page.remove_listener('response',observe)
    rows_by_id={identifier:[r for r in server_rows if r['job_id']==identifier] for identifier in observed_ids}
    server_rows=rows_by_id.get(str(req),[])
    # Full UI history plus one native exact ID for every ambiguous company/role
    # row can establish that those rows belong to DIFFERENT requisitions.
    other_ids=sorted(identifier for identifier in server_company_role if identifier!=str(req))
    disambiguated=bool(full and end['company_role']>0 and len(other_ids)==end['company_role'] and not server_rows)
    return {'full':full,'exact':max(end['exact'],1 if server_rows else 0) if full else 0,
            'loaded_rows':end['rows'],'expected_total':end['total'],'load_sweeps':sweeps,
            'company_role':0 if disambiguated else end['company_role'],
            'company_role_observed':end['company_role'],'company_role_disambiguated':disambiguated,
            'company_role_other_exact_ids':other_ids if disambiguated else [],
            'server_company_role_rows':list(server_company_role.values()) if full else [],
            'identity_basis':'server_history_exact_job_id' if server_rows and full else 'exact_listing_link',
            'server_exact_rows':server_rows if full else [],
            'exact_rows_by_id':rows_by_id if full else {},
            'observed_exact_rows_unverified':max(end['exact'],1 if server_rows else 0) if not full else 0,'at':_now()}

def history_match(history):
    """Partial history observations are never positive reconciliation evidence."""
    return history.get('full') is True and history.get('exact',0)>0

def confirmation_observations(page,url,req,company,title,*,wait_ms=15000,max_checks=3):
    """Bounded GET/list reads after an existing action; never reopen Apply or replay an answer."""
    observations=[]
    for index in range(max_checks):
        if index:page.wait_for_timeout(wait_ms)
        listing=_listing(page,url,req)
        if listing.get('site_restriction'):
            raise RuntimeError('Naukri CAPTCHA, account restriction or application limit requires handoff')
        history=_history(page,req,company,title)
        observations.append({'listing':listing,'history':history,'at':_now()})
        if history_match(history):break
        if not listing.get('exact_path') or not (listing.get('applied') or listing.get('pending')):break
    return listing,history,observations

def reconcile_delayed_confirmation(db,job,resume,history,config,*,bootstrap=False):
    """Record already-observed exact-job completion; never send an application action."""
    folder=ROOT/'jobs'/job['slug'];progress=_load_progress(folder)
    req=str(job['requisition_id']);attempt_id=resume['attempt_id']
    if not history_match(history):raise RuntimeError('Delayed confirmation requires fully loaded exact-ID history')
    if not progress or progress.get('attempt_id')!=attempt_id or str(progress.get('requisition_id'))!=req:
        raise RuntimeError('Delayed confirmation lacks matching durable questionnaire progress')
    stamp=_now();proof_path=folder/'naukri-native-confirmation.json'
    proof={'attempt_id':attempt_id,'requisition_id':req,'captured_at':stamp,'url':job['canonical_url'],
           'confirmation_text':'Exact Naukri requisition appears in fully loaded Applied Jobs history',
           'exact_history_id_match':True,'history_full':True,'delayed_confirmation':True,'new_application_action':False,
           'history_observed_at':history.get('at'),
           'history_identity':{'basis':history.get('identity_basis'),'server_rows':history.get('server_exact_rows',[])},
           'questionnaire_completion_verified':progress.get('pending_question') is None,
           'unverified_pending_question':progress.get('pending_question'),
           'newly_confirmed_answer_sent':False}
    proof_path.write_text(json.dumps(proof,indent=2)+'\n')
    row=db.execute('SELECT state FROM jobs WHERE id=?',(job['id'],)).fetchone()
    if row['state']=='NeedsAnswer':
        job_db.resume_incomplete_attempt(db,attempt_id,job['id'])
        job_db.transition(db,job['id'],'SubmissionUnconfirmed','delayed_history_reconciliation',
                          'Exact-job completion observed; no new browser action',str(proof_path.relative_to(ROOT)),'InProgress')
    job_db.confirm_attempt(db,attempt_id,job['id'],str(proof_path.relative_to(ROOT)),stamp,config=config,naukri_worker=True,bootstrap=bootstrap)
    _save_progress(folder,attempt_id,req,progress['answered'],None,status='confirmed',resumes=progress.get('resumes',0))
    return {'status':'Applied','attempt_id':attempt_id,'saves':len(progress['answered']),
            'confirmation_path':str(proof_path.relative_to(ROOT)),'delayed_confirmation':True,'new_application_action':False,
            'questionnaire_completion_verified':proof['questionnaire_completion_verified'],'newly_confirmed_answer_sent':False}

def _run_one(db,job,config,answer_profile,test_mode,resume=None,bootstrap=False):
    import job_config,control
    if not test_mode and (not job_config.live_naukri_enabled(config) or control.is_paused()):
        return {'job_id':job['id'],'status':'Paused' if control.is_paused() else 'DiscoveryOnly','submissions':0}
    job=dict(job);folder=ROOT/'jobs'/job['slug']
    ev=db.execute("SELECT path,sha256 FROM evidence WHERE job_id=? AND kind='resume_review' ORDER BY id DESC LIMIT 1",(job['id'],)).fetchone()
    if not ev:raise ValueError('Role-specific resume review evidence is missing')
    visual=json.loads((folder/'resume_visual_review.json').read_text())
    binding=validate_binding(job,ev['path'],visual,answer_profile,config,resume=bool(resume),checked_continuation=bool(resume and resume.get('authorization_path')))
    if hashlib.sha256((ROOT/ev['path']).read_bytes()).hexdigest()!=ev['sha256']:
        raise ValueError('Role PDF changed after its evidence record')
    if test_mode:return {'job_id':job['id'],'requisition_id':binding['requisition_id'],
        'status':'WouldResume' if resume else 'WouldSubmit','would_submit':True,'would_resume':bool(resume),'resume_filename':binding['resume_filename'],
        'global_submissions_enabled':bool(config['submissions']['enabled']),'actual_submission':False}
    # Claim/attempt row is created only after exact live/listing/history checks and a confirmed answer is visible.
    result={'job_id':job['id'],'requisition_id':binding['requisition_id'],'attempt_id':None,'saves':0,'status':'Preflight','stage':'browser_start'}
    outcome_path=folder/'naukri-native-outcome.json';presubmit_path=folder/'naukri-native-presubmit.json'
    attempt_id=None;phase=None;pending=[];page=None;context=None
    answered=list(resume['answered']) if resume else [];resumes=resume.get('resumes',0) if resume else 0
    state={'page':None,'restore_needed':False,'restored':None}
    from naukri_direct_playwright import _status
    from naukri_phase_guard import NaukriPhaseGuard
    try:
        with _browser_session(state) as context:
            page=context.pages[0] if context.pages else context.new_page();state['page']=page
            result['stage']='session_check'
            if not _status(page)['signed_in']:raise RuntimeError('Isolated Naukri profile is signed out')
            if not page.evaluate("()=>/Yugal\\s+Teotia/i.test(document.body.innerText)"):raise RuntimeError('Signed-in account identity mismatch')
            startup_path=folder/'naukri-generic-startup-verification.json'
            result.update(preflight_resume_repair_only=True,generic_resume_restored=False,
                          generic_resume_restore_evidence=str(startup_path.relative_to(ROOT)))
            startup=ensure_generic_resume(page,job,binding['requisition_id'],startup_path)
            result.update(preflight_resume_repair_only=False,generic_resume_restored=True,
                          generic_resume_sha256=startup['expected_sha256'],generic_resume_verified_at=startup['verified_at'],
                          generic_resume_repaired_at_start=startup['repair_performed'])
            url=job['canonical_url'];req=binding['requisition_id']
            result['stage']='listing_check';listing=_listing(page,url,req)
            if listing.get('site_restriction'):raise RuntimeError('Naukri CAPTCHA, account restriction or application limit requires handoff')
            result['stage']='applied_history_check';history=_history(page,req,job['employer'],job['title'])
            (folder/'naukri-native-history-preflight.json').write_text(json.dumps(history,indent=2)+'\n')
            if resume and listing['exact_path'] and listing['applied']:
                if not history_match(history):
                    if job['state']=='NeedsAnswer':
                        job_db.resume_incomplete_attempt(db,resume['attempt_id'],job['id'])
                        job_db.transition(db,job['id'],'SubmissionUnconfirmed','delayed_history_reconciliation',
                                          'Applied listing lacks complete exact-ID history; do not retry',None,'InProgress')
                    _save_progress(folder,resume['attempt_id'],req,answered,resume.get('pending_question'),
                                   status='uncertain',resumes=resumes)
                    result.update(status='Uncertain',attempt_id=resume['attempt_id'],
                                  reason='Applied on listing — reconciliation pending',
                                  reconciliation_pending=True,new_application_action=False)
                    return result
                result.update(reconcile_delayed_confirmation(db,job,resume,history,config,bootstrap=bootstrap))
                outcome_path.write_text(json.dumps(result|{'answers_logged':False},indent=2)+'\n')
                return result
            if not listing['exact_path'] or listing['apply_count']!=1 or listing['applied'] or listing['pending']:
                raise RuntimeError('Exact listing lacks a unique live Apply action or is already applied/pending')
            if not history['full']:raise RuntimeError('Applied Jobs history is incomplete; load the full history before applying')
            if history['exact']:raise RuntimeError('Exact requisition already exists in Applied Jobs history; do not reapply')
            if history['company_role']:raise RuntimeError('Applied Jobs history has unresolved company/title matches; verify their exact requisition IDs before applying')
            state['restore_needed']=True;_write_marker(job['id'],req)
            result['stage']='role_resume_upload'
            uploaded=upload_profile(page,ROOT/ev['path'])
            if uploaded['after']!=binding['resume_filename']:raise RuntimeError('Role-specific profile resume did not verify')
            # Recheck exact listing after profile swap, immediately before opening Apply.
            listing=_listing(page,url,req)
            if not listing['exact_path'] or listing['apply_count']!=1 or listing['applied'] or listing['pending']:
                raise RuntimeError('Listing changed before application flow opened')
            if resume:
                import control
                if control.is_paused():
                    result.update(status='Paused');return result
                if resume.get('authorization_path'):
                    job_db.resume_authorized_uncertain_attempt(db,resume['attempt_id'],job['id'],resume['authorization_path'])
                else:job_db.resume_incomplete_attempt(db,resume['attempt_id'],job['id'])
                attempt_id=resume['attempt_id'];job['state']='InProgress';resumes+=1
                _save_progress(folder,attempt_id,req,answered,resume.get('pending_question'),status='in_progress',resumes=resumes)
            phase=NaukriPhaseGuard(req)
            if resume:phase.claim_before_open(attempt_id)
            def on_response(response):
                path=urlsplit(response.url).path
                if path not in (APPLY_PATH,CHAT_PATH):return
                event=next((x for x in pending if x.get('path')==path and x.get('response_status') is None),None)
                meta={}
                try:
                    body=response.json();node=body.get('chatbotResponse',{}) if isinstance(body,dict) else {}
                    meta={k:node.get(k) for k in ('isApply','isLeafNode','dataCommitted','currentNodeName')}
                    meta['job_id_match']=req in json.dumps(body,ensure_ascii=False)
                except Exception:pass
                phase.response(event,response.status,meta)
            page.on('response',on_response)
            expect={'text':None,'observe':None,'payloads':[]}
            def route_guard(route):
                request=route.request;path=urlsplit(request.url).path
                if request.method in ('GET','HEAD','OPTIONS'):route.continue_();return
                if path in (APPLY_PATH,CHAT_PATH) and (control.is_paused() or not job_config.live_naukri_enabled(job_config.load())):
                    pending.append(phase._event(path,request.method,None,False,'live_control_disabled'));route.abort();return
                try:payload=request.post_data_json if path in (APPLY_PATH,CHAT_PATH) else None
                except Exception:payload=None
                if path==CHAT_PATH and expect['text'] is None and expect.get('observe') is not None:
                    seen=(payload.get('input') or {}).get('text') if isinstance(payload,dict) and isinstance(payload.get('input'),dict) else None
                    if isinstance(seen,list) and len(seen)==1:seen=seen[0]
                    # Observation only (a boolean, never the value): learn the real text-answer payload shape for a later strict check.
                    result.setdefault('payload_answer_matches',[]).append(isinstance(seen,str) and seen.strip()==expect['observe'].strip())
                if path==CHAT_PATH and expect['text'] is not None:
                    given=(payload.get('input') or {}).get('text') if isinstance(payload,dict) and isinstance(payload.get('input'),dict) else None
                    if not any(_payload_matches(given,p) for p in expect['payloads'] or [expect['text']]):
                        pending.append(phase._event(path,request.method,None,False,'unconfirmed_chat_answer'));route.abort();return
                ok,event=phase.decide(request.method,request.url,payload)
                if event:pending.append(event)
                if path==CHAT_PATH and ok:
                    facts=[x for x in result.get('question_facts',[]) if x.get('ready')]
                    if not facts:route.abort();return
                    facts[-1]['ready']=False
                route.continue_() if ok else route.abort()
            context.route('**/*',route_guard)
            apply=page.locator('#apply-button:visible').filter(has_text='Apply')
            if apply.count()!=1:raise RuntimeError('Apply action is ambiguous')
            if not resume:
                import control
                if control.is_paused():result.update(status='Paused');return result
                initial=folder/'naukri-native-open-review.json'
                initial.write_text(json.dumps({'job_id':job['id'],'requisition_id':req,'form_url':url,
                    'provider':'naukri_native_chat','signed_in':True,'account_name_matches':True,
                    'resume_filename':binding['resume_filename'],'resume_sha256':binding['resume_sha256'],
                    'final_action':'native_apply_open','visible_required_fields':0,'answers_logged':False,
                    'checks':{'live':True,'unapplied':True,'fit':True,'resume_visual':True,
                              'resume_selected':True,'answers_confirmed':True,'unique_apply':True},'created_at':_now()},indent=2)+'\n')
                if job['state']=='ResumeReady':
                    for next_state in ('InProgress','Preparing','Prepared'):
                        job_db.transition(db,job['id'],next_state,'naukri_native_worker','Exact job and role resume verified before native Apply',str(initial.relative_to(ROOT)),job['state'])
                        job['state']=next_state
                resume_hash=job_db.record_evidence(db,job['id'],'resume_review',ev['path'])
                form_hash=job_db.record_evidence(db,job['id'],'form_review',str(initial.relative_to(ROOT)))
                attempt_id=job_db.begin_attempt(db,job['id'],ev['path'],resume_hash,url,str(initial.relative_to(ROOT)),form_hash,config=config,naukri_worker=True,bootstrap=bootstrap)
                phase.claim_before_open(attempt_id);result['attempt_id']=attempt_id;job['state']='SubmissionUnconfirmed'
                _save_progress(folder,attempt_id,req,answered,None,status='in_progress',resumes=resumes,save_in_flight=True)
            apply.click(timeout=10000);page.wait_for_timeout(3500)
            result['stage']='questionnaire'
            ui=_chat(page)
            if ui.get('site_restriction'):raise RuntimeError('Naukri CAPTCHA, account restriction or application limit requires handoff')
            if phase.phase not in ('claimed','chat_ready') and not phase.workflow_committed:
                raise RuntimeError('Exact job native Apply did not receive its guarded response')
            if resume:result['attempt_id']=attempt_id
            result['question_facts']=[];saved_answers=[];answered_keys={a['question_key'] for a in answered if a.get('question_key')}
            legacy_answered={a['question'] for a in answered if not a.get('question_key')}

            def needs_input(reason,question,message,**extra):
                """Stop for a missing answer/format. A claimed attempt becomes safely incomplete only after read-only proof."""
                payload={'job_id':job['id'],'requisition_id':req,'url':url,'reason':reason,'question':question,
                         'at':_now(),'answers_logged':False,**extra}
                if attempt_id:
                    verdict=_verify_incomplete(page,phase,url,req,job,folder,attempt_id,question)
                    if not verdict['safe']:
                        result['unsafe_reason']='claimed questionnaire could not be proven safely incomplete'
                        raise RuntimeError('Claimed questionnaire could not be proven safely incomplete')
                    _save_progress(folder,attempt_id,req,answered,question,status='incomplete',resumes=resumes)
                    payload.update(attempt_id=attempt_id,progress=str(_progress_path(folder).relative_to(ROOT)),
                                   incomplete_evidence=verdict['evidence_path'],answered_count=len(answered))
                    outcome_path.write_text(json.dumps(payload,indent=2,ensure_ascii=False)+'\n')
                    job_db.mark_attempt_incomplete(db,attempt_id,job['id'],verdict['evidence_path'])
                    result.update({'status':'NeedsAnswer','blocker':reason,'question':question,'incomplete':True,'answered':len(answered)})
                    return result
                outcome_path.write_text(json.dumps(payload,indent=2,ensure_ascii=False)+'\n')
                if job['state'] in ('Prepared','ResumeReady'):
                    job_db.transition(db,job['id'],'NeedsAnswer','naukri_native_worker',message,str(outcome_path.relative_to(ROOT)),job['state'])
                result.update({'status':'NeedsAnswer','blocker':reason,'question':question});return result
            while len(answered)<MAX_QUESTIONS:
                ui=_chat(page)
                if ui.get('site_restriction'):raise RuntimeError('Naukri CAPTCHA, account restriction or application limit requires handoff')
                if not ui['drawer'] or not ui['prompts']:break  # native applications may have no screening questions
                if ui['thank_you']:
                    if attempt_id and _reopen(page,phase,url,req):continue
                    break
                question=ui['prompts'][-1] if ui['prompts'] else None
                question_key=_question_key(ui,phase)
                if question_key in answered_keys or question in legacy_answered:
                    if history_match(_history(page,req,job['employer'],job['title'])):break
                    raise RuntimeError('Question repeated after continuation; no answer is replayed')
                choice=ui['editor_count']==0 and bool(ui['options'] or ui.get('selects'))
                if question:result['pending_question']=question
                if (not question or (ui['editor_count']!=1 and not choice) or ui['save_count']!=1
                        or ui['save_label']!='Save'):
                    if question:
                        return needs_input('unsupported_question_format',question,'Questionnaire format needs manual handling',editor_count=ui['editor_count'],save_count=ui['save_count'])
                    raise RuntimeError('Question, editor, or potentially submitting Save was not uniquely identified')
                answer,fact_ref=_answer(question,answer_profile)
                if answer is None and ui.get('options') and all(o.get('type')=='checkbox' for o in ui['options']):
                    import confirmed_forms
                    answer,fact_ref=confirmed_forms.answer({'question':question,'type':'checkbox','options':[{'label':o['text']} for o in ui['options']]},answer_profile)
                if not answer:
                    return needs_input('unknown_required_answer',question,'Required questionnaire answer is not in confirmed profile')
                import confirmed_forms
                conflict=confirmed_forms.validate_answer({'question':question},answer)
                if conflict:return needs_input(conflict,question,'Experience answer needs reconciliation with confirmed employment dates')
                confirmed,payloads=_stage_answer(page,ui,answer)
                if not confirmed:return needs_input('confirmed_answer_not_accepted_by_control',question,'Confirmed answer is not among the options or did not remain in the exact control')
                expect.update(text=answer,observe=None,payloads=payloads)
                staged=_chat(page)
                if staged['save_count']!=1 or staged['save_label']!='Save' or staged['save_disabled']:
                    return needs_input('save_control_not_ready',question,'Confirmed answer did not enable the exact Save control')
                result['question_facts'].append({'fact_ref':fact_ref,'ready':True})
                checks={'live':True,'unapplied':True,'fit':True,'resume_visual':True,'resume_selected':True,
                        'answers_confirmed':True,'unique_apply':True}
                packet={'job_id':job['id'],'requisition_id':req,'form_url':url,'provider':'naukri_native_chat',
                    'signed_in':True,'account_name_matches':True,'resume_filename':binding['resume_filename'],
                    'resume_sha256':binding['resume_sha256'],'final_action':'screening_chat_save','checks':checks,
                    'facts_used':[x['fact_ref'] for x in result['question_facts']],'answers_logged':False,'created_at':_now()}
                presubmit_path.write_text(json.dumps(packet,indent=2,ensure_ascii=False)+'\n')
                resume_hash=job_db.record_evidence(db,job['id'],'resume_review',ev['path'])
                form_hash=job_db.record_evidence(db,job['id'],'form_review',str(presubmit_path.relative_to(ROOT)))
                if not attempt_id:
                    import control
                    if control.is_paused():
                        result.update(status='Paused');return result
                    if job['state']=='ResumeReady':
                        job_db.transition(db,job['id'],'InProgress','naukri_native_worker','Live listing, full history and exact role resume verified',str(presubmit_path.relative_to(ROOT)),'ResumeReady')
                        job_db.transition(db,job['id'],'Preparing','naukri_native_worker','Confirmed answer staged for exact Naukri questionnaire',str(presubmit_path.relative_to(ROOT)),'InProgress')
                        job_db.transition(db,job['id'],'Prepared','naukri_native_worker','Exact live Naukri form and resume checks passed',str(presubmit_path.relative_to(ROOT)),'Preparing')
                    elif job['state']!='Prepared':
                        raise RuntimeError('Job state is not ready for the final questionnaire action')
                    attempt_id=job_db.begin_attempt(db,job['id'],ev['path'],resume_hash,url,
                        str(presubmit_path.relative_to(ROOT)),form_hash,config=config,naukri_worker=True,bootstrap=bootstrap)
                    phase.claim(attempt_id);result['attempt_id']=attempt_id
                if job['state']=='InProgress':
                    job_db.transition(db,job['id'],'SubmissionUnconfirmed','naukri_native_worker','Continuation Save is about to be sent; claim retained',str(presubmit_path.relative_to(ROOT)),'InProgress');job['state']='SubmissionUnconfirmed'
                _save_progress(folder,attempt_id,req,answered,question,status='in_progress',resumes=resumes,save_in_flight=True)
                phase.arm_save(action_label=ui['save_label'],exact_chat=ui['drawer'],confirmed_answer=confirmed)
                result['saves']+=1;outcome_path.write_text(json.dumps({'job_id':job['id'],'requisition_id':req,'attempt_id':attempt_id,'saves':result['saves'],'answers_logged':False,'status':'SaveSentAwaitingProof','at':_now()},indent=2)+'\n')
                page.locator('.chatbot_Drawer .sendMsgbtn_container').click(timeout=10000)
                for _ in range(7):
                    page.wait_for_timeout(1000);probe=_chat(page)
                    if probe['thank_you'] or phase.workflow_committed or (probe['prompts'] and probe['prompts'][-1]!=question):break
                saved_answers.append(fact_ref);answered_keys.add(question_key);after=_chat(page)
                def record_saved(next_question,status):
                    answered.append({'question':question,'question_key':question_key,'fact_ref':fact_ref,'at':_now()})
                    _save_progress(folder,attempt_id,req,answered,next_question,status=status,resumes=resumes,save_in_flight=False)
                if phase.workflow_committed or after['thank_you']:
                    record_saved(None,'awaiting_proof')
                    # A saved screening answer can close chat before the native
                    # application finishes. Continue the same in-flight claim only
                    # while the exact listing is still unapplied; never replay answers.
                    if not phase.workflow_committed and _reopen(page,phase,url,req):continue
                    break
                if after['prompts'] and after['prompts'][-1]!=question:
                    phase.acknowledge_next_question(question_changed=True,editor_empty=all(after['editor_empty']),save_visible=after['save_count']==1)
                    record_saved(after['prompts'][-1],'in_progress');continue
                if _reopen(page,phase,url,req):
                    again=_chat(page)
                    if again['thank_you'] or (again['prompts'] and again['prompts'][-1]!=question):
                        record_saved(again['prompts'][-1] if again['prompts'] and not again['thank_you'] else None,'in_progress');continue
                raise RuntimeError('Save result is uncertain; no further Save will be sent')
            if not attempt_id:raise RuntimeError('No confirmed questionnaire answer; attempt was not claimed')
            result['stage']='confirmation_check'
            listing_after,history_after,observations=confirmation_observations(page,url,req,job['employer'],job['title'])
            confirmation_audit=folder/'naukri-native-confirmation-observations.json'
            confirmation_audit.write_text(json.dumps({'attempt_id':attempt_id,'requisition_id':req,'observations':observations},indent=2)+'\n')
            result.update(confirmation_observations_path=str(confirmation_audit.relative_to(ROOT)),
                          exact_listing_applied=bool(listing_after.get('exact_path') and listing_after.get('applied')),
                          history_full=history_after['full'],history_exact=history_after['exact'])
            commit=_job_bound_commit(phase,req,listing_after,history_after)
            positive=history_match(history_after)
            if commit and positive:
                stamp=_now();proof_path=folder/'naukri-native-confirmation.json'
                proof={'attempt_id':attempt_id,'requisition_id':req,'captured_at':stamp,'url':url,
                       'confirmation_text':'Exact Naukri requisition appears as Applied','exact_listing_applied':listing_after['applied'],
                       'exact_history_id_match':history_match(history_after),'history_full':history_after['full'],'network_reason':commit['reason'],
                       'history_identity':{'basis':history_after.get('identity_basis'),'server_rows':history_after.get('server_exact_rows',[])},
                       'network_status':commit['response_status'],'flowtype':commit['flowtype'],
                       'response_metadata':commit.get('response_metadata',{})}
                proof_path.write_text(json.dumps(proof,indent=2)+'\n')
                job_db.confirm_attempt(db,attempt_id,job['id'],str(proof_path.relative_to(ROOT)),stamp,config=config,naukri_worker=True,bootstrap=bootstrap)
                result.update({'status':'Applied','confirmation_path':str(proof_path.relative_to(ROOT))})
                _save_progress(folder,attempt_id,req,answered,None,status='confirmed',resumes=resumes)
            else:
                result.update({'status':'Uncertain','completion_2xx':bool(commit),'job_bound_confirmation':positive})
                if listing_after['exact_path'] and listing_after['applied']:
                    result['reconciliation_pending']=True;result['reason']='applied_on_listing_without_independent_history_confirmation'
                _save_progress(folder,attempt_id,req,answered,result.get('pending_question'),status='uncertain',resumes=resumes)
            outcome_path.write_text(json.dumps({k:v for k,v in result.items() if k!='question_facts'}|{'answers_logged':False,'network_guard':phase.summary()},indent=2,ensure_ascii=False)+'\n')
            return result
    except Exception as exc:
        result.update({'status':'Uncertain' if attempt_id else 'Blocked','error_type':type(exc).__name__})
        if isinstance(exc,RuntimeError) and re.search(r'captcha|restriction|application limit|signed out',str(exc),re.I):result['provider_stop_reason']='site_verification_restriction_or_limit'
        import traceback
        frame=traceback.extract_tb(exc.__traceback__)[-1]
        result['error_origin']={'file':Path(frame.filename).name,'function':frame.name,'line':frame.lineno}
        if isinstance(exc,RuntimeError) and Path(frame.filename).name in ('naukri_native_worker.py','infosys_resume_flow.py'):
            # These modules raise controlled errors; remove URLs and known
            # profile values before persisting an actionable explanation.
            from ui_api import scrub
            result['error_message']=scrub(re.sub(r'https?://\S+','[URL omitted]',str(exc)))
        # Keep technical diagnostics useful without retaining page URLs, form values or credentials.
        first=str(exc).splitlines()[0]
        for category in ('syntaxerror','referenceerror','typeerror','strict mode violation','outside of the viewport','element is not attached','element is not visible'):
            if category in first.lower():result['technical_error']=category;break
        outcome_path.write_text(json.dumps({k:v for k,v in result.items() if k!='question_facts'}|{'answers_logged':False},indent=2)+'\n')
        if attempt_id:
            outcome_path.write_text(json.dumps({k:v for k,v in result.items() if k!='question_facts'}|{'answers_logged':False,'network_guard':phase.summary() if phase else None},indent=2)+'\n')
        elif isinstance(exc,RuntimeError) and any(word in str(exc).lower() for word in ('signed out','account identity','apply action','captcha','listing changed','question','browser')):
            blocker={**result,'job_id':job['id'],'requisition_id':binding['requisition_id'],'url':binding['url'],
                     'reason':'application_handoff_required','error_type':type(exc).__name__,
                     'detail':str(exc)[:200] if isinstance(exc,RuntimeError) else None,'at':_now()}
            outcome_path.write_text(json.dumps(blocker,indent=2)+'\n')
            try:
                current=db.execute('SELECT state FROM jobs WHERE id=?',(job['id'],)).fetchone()['state']
                if current in ('Prepared','ResumeReady'):
                    job_db.transition(db,job['id'],'NeedsAnswer','naukri_native_worker','Naukri UI/login handoff required',str(outcome_path.relative_to(ROOT)),current)
                result['status']='NeedsAnswer';result['blocker']='application_handoff_required'
            except Exception:pass
        return result
    finally:
        if phase and any(e.get('response_status') in (403,429) for e in phase.events):result['provider_stop_reason']='site_restriction_or_limit'
        if state['restore_needed']:
            result['generic_resume_restored']=bool(state['restored'])
            result['generic_resume_sha256']=state.get('restore_sha256')
            if outcome_path.exists():
                try:
                    saved=json.loads(outcome_path.read_text())
                    saved['generic_resume_restored']=result['generic_resume_restored']
                    saved['generic_resume_sha256']=state.get('restore_sha256')
                    saved['generic_resume_verified_at']=_now()
                    outcome_path.write_text(json.dumps(saved,indent=2,ensure_ascii=False)+'\n')
                except Exception:pass

def _note_block(db,row):
    """Back off a job that stopped before any claim; escalate to a user handoff after repeated stops."""
    retry=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=RETRY_AFTER_BLOCK_HOURS)).isoformat()
    db.execute('UPDATE queue SET retry_after=?,attempts=attempts+1 WHERE job_id=?',(retry,row['id']))
    attempts=db.execute('SELECT attempts FROM queue WHERE job_id=?',(row['id'],)).fetchone()
    state=db.execute('SELECT state FROM jobs WHERE id=?',(row['id'],)).fetchone()['state']
    if attempts and attempts['attempts']>=MAX_BLOCKED_TRIES and state in ('Prepared','ResumeReady'):
        outcome=ROOT/'jobs'/row['slug']/'naukri-native-outcome.json'
        evidence=str(outcome.relative_to(ROOT)) if outcome.is_file() else None
        job_db.transition(db,row['id'],'NeedsAnswer','naukri_native_worker','Repeated pre-claim stops; manual review needed',evidence,state)

def save_live_receipt(db,job_id):
    """Promote only a real confirmed application with complete history and verified restoration."""
    row=db.execute("SELECT a.*,j.slug,j.requisition_id,j.state job_state FROM submission_attempts a JOIN jobs j ON j.id=a.job_id WHERE a.job_id=? AND a.state='confirmed'",(job_id,)).fetchone()
    if not row or row['job_state']!='Applied':raise ValueError('Confirmed Applied attempt required')
    proof=ROOT/row['proof_path'];p=json.loads(proof.read_text())
    outcome=json.loads((ROOT/'jobs'/row['slug']/'naukri-native-outcome.json').read_text())
    if not p.get('history_full') or not p.get('exact_history_id_match') or outcome.get('generic_resume_restored') is not True or outcome.get('generic_resume_sha256')!=_sha(GENERIC):
        raise ValueError('Complete exact-ID history and current generic restoration required')
    receipt={'schema':1,'status':'verified','provider':'naukri_portal_native','job_id':job_id,
        'requisition_id':row['requisition_id'],'attempt_id':row['attempt_id'],'proof_path':row['proof_path'],
        'proof_sha256':row['proof_sha256'],'role_resume_sha256':row['resume_sha256'],
        'application_confirmed':True,'generic_resume_restored':True,'generic_resume_sha256':_sha(GENERIC),
        'independently_reconciled':True,'confirmation_basis':['fully_loaded_applied_history_exact_job_id'],
        'verified_at':_now(),'unattended_scheduled_success_proven':False}
    for name in ('full-application-path-verified.json','naukri-live-verified.json'):
        (ROOT/'.workflow'/name).write_text(json.dumps(receipt,indent=2)+'\n')
    return receipt

def run(db,config,*,test_mode=False,max_submissions=1,application_job_id=None):
    budget=AttemptBudget(max_submissions)
    if max_submissions>config['portal_workers']['naukri']['max_submissions_per_run']:raise ValueError('Naukri attempt limit exceeds configured cap')
    import job_config
    if not test_mode and not job_config.live_naukri_enabled(config):
        return {'status':'DiscoveryOnly','candidate_count':0,'submissions':0,'forms_opened':0,'uploads':0}
    if not config.get('naukri_worker',{}).get('enabled'):return {'status':'Disabled','candidates':0,'submissions':0}
    if not application_job_id and not job_db.naukri_worker_path_verified(db):
        return {'status':'Blocked','candidate_count':0,'submissions':0,'forms_opened':0,'uploads':0,
                'blocker':'direct_naukri_application_path_proof_missing'}
    if _marker().is_file():
        if test_mode:
            return {'status':'Blocked','candidate_count':0,'submissions':0,'forms_opened':0,'uploads':0,'blocker':'profile_restore_pending'}
        recovered=recover_pending_restore()
        if recovered['status']=='Failed':
            from urgent_alerts import desktop, TITLE
            desktop(TITLE,'A role resume may still be the active Naukri profile resume. Restore and verify Yugal_Teotia_CV.pdf.')
            return {'status':'Blocked','candidate_count':0,'submissions':0,'forms_opened':0,'uploads':0,
                    'blocker':'profile_restore_pending','recovery':recovered}
    answer_profile=(ROOT/'APPLICATION_PROFILE.md').read_text()
    rows=candidates(db,config)
    resumes=resumable(db,config,answer_profile)
    if application_job_id is not None:
        rows=[r for r in rows if r['id']==application_job_id]
        resumes=[(r,p) for r,p in resumes if r['id']==application_job_id]
        # This marker is created only from an explicit new user authorization.
        # Scheduled/default runs cannot discover or consume an uncertain continuation.
        job=db.execute('SELECT j.*,a.attempt_id claimed_attempt FROM jobs j JOIN submission_attempts a ON a.job_id=j.id WHERE j.id=? AND j.state=\'SubmissionUnconfirmed\' AND a.state=\'uncertain\'',(application_job_id,)).fetchone()
        if job:
            auth_path=ROOT/'.workflow/authorized-continuations'/f'{job["claimed_attempt"]}.json'
            if auth_path.is_file():
                auth=json.loads(auth_path.read_text());p=_load_progress(ROOT/'jobs'/job['slug'])
                if auth.get('approved_by')=='user' and not auth.get('used_at') and p and p.get('resumes',0)<MAX_RESUMES:
                    resumes=[(job,{**p,'authorization_path':str(auth_path.relative_to(ROOT))})]
    if test_mode:
        inspected=[]
        for row,progress in resumes:
            try:inspected.append(_run_one(db,row,config,answer_profile,True,resume=progress))
            except Exception as exc:inspected.append({'job_id':row['id'],'status':'NotReady','reason':type(exc).__name__,'would_resume':True})
        for row in rows[:max_submissions]:
            ev=db.execute("SELECT path FROM evidence WHERE job_id=? AND kind='resume_review' ORDER BY id DESC LIMIT 1",(row['id'],)).fetchone()
            if not ev:continue
            try:
                review=json.loads((ROOT/'jobs'/row['slug']/'resume_visual_review.json').read_text())
                inspected.append(_run_one(db,row,config,answer_profile,True))
            except Exception as exc:
                inspected.append({'job_id':row['id'],'status':'NotReady','reason':type(exc).__name__})
        return {'status':'DryRun','candidate_count':len(rows),'resumable_count':len(resumes),'checks':inspected,'submissions':0,'forms_opened':0,'uploads':0}
    results=[];submitted=0;provider_stop=False
    work=[(row,progress) for row,progress in resumes]+[(row,None) for row in rows]
    for row,progress in work:
        import control
        if control.is_paused():results.append({'status':'Paused'});break
        prior_claim=claim_id(db,row['id'])
        if not budget.can_start(prior_claim) or provider_stop or len(results)>=MAX_JOBS_TRIED_PER_RUN:break
        if results:pace()
        kwargs={'bootstrap':True} if application_job_id else {}
        try:result=_run_one(db,row,config,answer_profile,False,resume=progress,**kwargs)
        except ValueError as exc:result=preflight_failure(row,exc,ROOT,'naukri',None if prior_claim else claim_id(db,row['id']))
        results.append(result)
        budget.record(result,prior_claim,claim_id(db,row['id']))
        if result.get('status')=='Paused':break
        if result.get('status')=='Blocked' and not result.get('attempt_id') and progress is None:_note_block(db,row)
        if result.get('status')=='Applied':
            submitted+=1
            if result.get('generic_resume_restored') is True:save_live_receipt(db,row['id'])
            from urgent_alerts import resolve_sqlite_confirmed_blocker
            resolve_sqlite_confirmed_blocker(db,row['id'])
        if result.get('provider_stop_reason') or result.get('stage')=='session_check' and result.get('status') not in ('Applied','Preflight'):provider_stop=True
        if result.get('generic_resume_restored') is False:
            from urgent_alerts import desktop, TITLE, job_link
            notice=desktop(TITLE,'Restore and verify the generic Naukri resume before another application.\n'+job_link(row['canonical_url']))
            result['generic_resume_restore_alert_sent']=bool(notice.get('notification_sent'))
            provider_stop=True
    return {'status':'Complete' if results else 'Idle','candidate_count':len(rows),'resumable_count':len(resumes),'results':results,'submissions':submitted,
            'blocked_jobs':sum(x.get('status') in ('Blocked','NeedsAnswer') for x in results),'provider_stopped':provider_stop,**budget.summary()}
