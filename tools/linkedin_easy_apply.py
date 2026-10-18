"""Guarded LinkedIn Easy Apply, using the dedicated automation session only."""
import hashlib
import contextlib
import json
import re
import uuid
import traceback
from urllib.parse import urlsplit,parse_qs
import candidate
import confirmed_forms as forms
import control
import fit_engine
import job_config
import job_db
from application_budget import AttemptBudget,claim_id,pace,preflight_failure
from linkedin_browser import chrome,status
from linkedin_discovery import details_url
from local_paths import ROOT

MODAL='.artdeco-modal:visible,[role=dialog]:visible'
MAX_STEPS=20

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def exact(page,req):
    u=urlsplit(page.url)
    return u.hostname=='www.linkedin.com' and (u.path.rstrip('/')=='/jobs/view/'+str(req) or parse_qs(u.query).get('currentJobId')==[str(req)])

def restriction(page):
    return page.evaluate('''()=>{const t=document.body.innerText;return /captcha|security verification|verify your identity|account (?:restricted|suspended)|daily application limit|reached.*application limit|too many requests/i.test(t)}''')

def candidates(db,config):
    # Only unclaimed drafts can resume. A possibly submitted draft is permanently excluded.
    profile=(ROOT/'APPLICATION_PROFILE.md').read_text()
    for row in db.execute("SELECT * FROM jobs WHERE source='linkedin_portal_native' AND state='NeedsAnswer' AND NOT EXISTS(SELECT 1 FROM submission_attempts a WHERE a.job_id=jobs.id)").fetchall():
        path=ROOT/'jobs'/row['slug']/'linkedin-easy-apply-progress.json'
        if not path.exists():continue
        saved=json.loads(path.read_text());field=saved.get('pending_field')
        if saved.get('submit_clicked') or saved.get('reconciliation_pending') or not field or saved.get('pending_reason')!='unknown_required_answer':continue
        if forms.answer(field,profile)[0] is not None:
            job_db.transition(db,row['id'],'ResumeReady','linkedin_easy_apply','Confirmed answer now available for the same unsubmitted draft',str(path.relative_to(ROOT)),'NeedsAnswer')
            db.execute("UPDATE queue SET state='ResumeReady',retry_after=NULL WHERE job_id=?",(row['id'],))
    return db.execute("""SELECT j.* FROM jobs j JOIN queue q ON q.job_id=j.id
        WHERE j.source='linkedin_portal_native' AND j.state IN ('ResumeReady','Prepared') AND j.availability='Live'
        AND j.fit IN ('High','Medium') AND q.state='ResumeReady'
        AND (q.retry_after IS NULL OR q.retry_after<=?)
        AND NOT EXISTS(SELECT 1 FROM submission_attempts a WHERE a.job_id=j.id)
        AND NOT EXISTS(SELECT 1 FROM history_keys h WHERE h.employer_key=j.employer_key AND h.requisition_key=j.requisition_key)
        ORDER BY (j.fit='High') DESC,q.priority DESC LIMIT 30""",(job_db.now(),)).fetchall()

def binding(db,job,config):
    saved=json.loads(job['fit_json'] or '{}');verdict=fit_engine.evaluate(dict(job),saved.get('decision') or {},config)
    if not (verdict['eligible'] or verdict.get('strong_medium_eligible')):raise ValueError('Current essential-skill/experience evidence does not pass')
    if json.loads(job['provenance_json'] or '{}').get('duplicate_check',{}).get('status')!='clear':raise ValueError('Duplicate clearance missing')
    if db.execute('SELECT 1 FROM submission_attempts WHERE job_id=?',(job['id'],)).fetchone():raise ValueError('Any existing attempt blocks a new application')
    ev=db.execute("SELECT path,sha256 FROM evidence WHERE job_id=? AND kind='resume_review' ORDER BY id DESC LIMIT 1",(job['id'],)).fetchone()
    if not ev:raise ValueError('Exact role PDF missing')
    pdf=(ROOT/ev['path']).resolve();folder=(ROOT/'jobs'/job['slug']).resolve()
    visual=json.loads((folder/'resume_visual_review.json').read_text())
    if not pdf.is_relative_to(folder) or sha(pdf)!=ev['sha256'] or visual.get('status')!='visually_clean' or visual.get('pdf_sha256')!=ev['sha256']:
        raise ValueError('Exact role PDF visual/hash binding failed')
    return ev,pdf

def _run_one(db,job,config,*,test_mode=False,bootstrap=False):
    job=dict(job);ev,pdf=binding(db,job,config);req=str(job['requisition_id'])
    if test_mode:return {'job_id':job['id'],'status':'WouldSubmit','actual_submission':False}
    if control.is_paused() or not job_config.live_provider_enabled(config,'linkedin'):return {'job_id':job['id'],'status':'PausedOrDisabled'}
    folder=ROOT/'jobs'/job['slug'];out=folder/'linkedin-easy-apply-outcome.json';progress_path=folder/'linkedin-easy-apply-progress.json'
    progress=json.loads(progress_path.read_text()) if progress_path.exists() else {'draft_id':str(uuid.uuid4()),'answered':[],'steps':0}
    # Contact restoration is distinct from accepted screening answers. LinkedIn may
    # clear an unsaved contact entity when reopening an unfinished application.
    progress.setdefault('contact_verified',[])
    progress['contact_verified']+= [x for x in progress['answered'] if str(x.get('fact_ref','')).startswith('confirmed_identity:')]
    progress['answered']=[x for x in progress['answered'] if not str(x.get('fact_ref','')).startswith('confirmed_identity:')]
    if progress.get('submit_clicked'):raise ValueError('Potential submission is locked; never reopen')
    result={'job_id':job['id'],'requisition_id':req,'status':'Preflight','attempt_id':None,'submit_clicked':False}
    attempt=None;submitted=False;resume_verified=False;page=None;answered={x['key']:x for x in progress['answered']}
    profile=(ROOT/'APPLICATION_PROFILE.md').read_text()
    user_review_path=ROOT/'.workflow/user-draft-review'/f'{job["id"]}.json'
    user_review=json.loads(user_review_path.read_text()) if user_review_path.exists() else {}
    user_review_active=bool(user_review.get('active') and str(user_review.get('requisition_id'))==req)
    user_filled_keys=set()
    def persist(question=None):
        progress.update(answered=list(answered.values()),pending_question=question,submit_clicked=submitted,updated_at=job_db.now())
        progress_path.write_text(json.dumps(progress,indent=2)+'\n')
    def needs(question,reason,field=None):
        progress['pending_field']=field
        progress['pending_reason']=reason
        if reason=='applied_on_listing_reconciliation_pending':progress['reconciliation_pending']=True
        persist(question);result.update(status='NeedsAnswer',question=question,reason=reason,url=job['canonical_url'])
        if not attempt:
            state=db.execute('SELECT state FROM jobs WHERE id=?',(job['id'],)).fetchone()[0]
            if state in ('ResumeReady','Prepared'):
                job_db.transition(db,job['id'],'NeedsAnswer','linkedin_easy_apply',reason,str(progress_path.relative_to(ROOT)),state)
        return result
    @contextlib.contextmanager
    def session():
        with chrome() as ctx:
            try:yield ctx
            finally:
                if page is not None and not submitted and not attempt and exact(page,req):
                    try:
                        dialog=page.locator(MODAL).last
                        dismiss=dialog.get_by_role('button',name=re.compile(r'^Dismiss$',re.I))
                        if dialog.is_visible() and dismiss.count()==1:
                            dismiss.click(timeout=5000);page.wait_for_timeout(500)
                            save_dialog=page.locator(MODAL).last
                            text=save_dialog.inner_text()
                            save=save_dialog.get_by_role('button',name=re.compile(r'^Save$',re.I))
                            if re.search(r'save (?:this|your) application',text,re.I) and save.count()==1:
                                save.click(timeout=5000);page.wait_for_timeout(700)
                                result['draft_save_requested']=True;progress['draft_save_requested']=True;persist(progress.get('pending_question'))
                    except Exception:result['draft_save_requested']=False
    try:
        with session() as c:
            page=c.pages[0] if c.pages else c.new_page()
            health=status(page)
            if not health['signed_in']:
                state=health.get('session_state','selector_or_render_failure')
                result['session_health']=health
                result['provider_stop_reason']=state
                if state=='authentication_required':return needs('Sign in to LinkedIn in the dedicated automation window','authentication_required')
                if state in ('verification_required','account_restricted'):
                    return needs('Complete the LinkedIn verification or resolve its account restriction','site_verification_or_limit')
                return needs('LinkedIn authenticated controls did not render; inspect the read-only session-health evidence','session_page_not_ready')
            page.goto(details_url(req),wait_until='domcontentloaded');page.wait_for_timeout(2500)
            if restriction(page):return needs('Complete LinkedIn site verification or resolve the account/site limit','site_verification_or_limit')
            if not exact(page,req):return needs('Inspect the exact LinkedIn listing','job_identity_changed')
            applied=page.get_by_text(re.compile(r'^Applied(?:\s+\d+.*)?$',re.I))
            if applied.count():return needs('Listing shows Applied; reconcile without retry','applied_on_listing_reconciliation_pending')
            native=page.locator('button.jobs-apply-button:visible,button[aria-label*="Easy Apply to"]:visible')
            easy=native.filter(has_text=re.compile('Easy Apply|Continue|Resume',re.I))
            if easy.count()!=1:
                result['native_apply_controls']=native.evaluate_all("els=>els.map(e=>({text:e.innerText.trim(),label:e.getAttribute('aria-label'),disabled:e.disabled}))")
                return needs('Exact listing has no unique Easy Apply action','external_apply_or_unavailable')
            if not re.search('Easy Apply',easy.inner_text(),re.I) and not progress.get('steps'):
                return needs('Inspect the existing native draft before continuing','unowned_existing_draft')
            def guard(route):
                request=route.request;u=urlsplit(request.url)
                if request.method in ('GET','HEAD','OPTIONS'):route.continue_();return
                if control.is_paused() or not job_config.live_provider_enabled(job_config.load(),'linkedin'):
                    route.abort();return
                if re.search(r'onsiteApplyApplications|jobApplications|submitApplication',u.path,re.I) and not attempt:
                    route.abort();return
                try:payload=request.post_data or ''
                except UnicodeDecodeError:payload=''  # Binary telemetry/upload bodies are not JSON application payloads.
                if re.search(r'jobPosting|jobId',payload,re.I):
                    ids=re.findall(r'(?:jobPosting[^\d]{0,35}|jobId[^\d]{0,15})(\d+)',payload,re.I)
                    if ids and any(x!=req for x in ids):route.abort();return
                route.continue_()
            c.route('**/*',guard)
            easy.click();page.wait_for_timeout(1500)
            for step in range(MAX_STEPS):
                if restriction(page):return needs('LinkedIn site verification or application limit','site_verification_or_limit')
                if control.is_paused():return needs('Workflow paused; continue the saved draft after resuming','paused')
                modal=page.locator(MODAL).last
                if not modal.is_visible():return needs('Inspect the same Easy Apply draft','application_dialog_missing')
                fields=page.evaluate(forms.FIELDS,'.artdeco-modal,[role=dialog]')
                result['visible_questions']=[{'question':f['question'],'type':f['type'],'required':f['required']} for f in fields]
                if user_review_active:
                    captured=[]
                    for field in fields:
                        value=forms.current_value(modal,field)
                        if value is not None and value!=[]:
                            captured.append({'key':forms.key(field),'question':field['question'],'type':field['type'],'answer':value})
                            user_filled_keys.add(forms.key(field))
                    if captured:
                        private=ROOT/'.workflow/private-candidate-confirmations';private.mkdir(parents=True,exist_ok=True);private.chmod(0o700)
                        path=private/('linkedin-'+req+'.json')
                        old=json.loads(path.read_text()).get('fields',[]) if path.exists() else []
                        merged={x['key']:x for x in old+captured}
                        path.write_text(json.dumps({'requisition_id':req,'confirmed_by_user':True,'confirmed_at':job_db.now(),'fields':list(merged.values())},indent=2)+'\n');path.chmod(0o600)
                        result['user_completed_fields_reviewed']=len(captured)
                staged=[]
                for field in fields:
                    result['active_control']=field['question']
                    if field['type']=='checkbox' and not field['required'] and re.match(r'Follow\s',field['question'],re.I):
                        forms.fill(modal,field,[]);continue
                    value,fact=forms.answer(field,profile,req);k=forms.key(field)
                    if value is not None:
                        conflict=forms.validate_answer(field,value)
                        if conflict:return needs(field['question'],conflict,field)
                        value=forms.format_answer(field,value)
                    if value is None:
                        if field['required']:return needs(field['question'],'unknown_required_answer',field)
                        continue
                    if not forms.fill(modal,field,value,verify_only=k in answered or k in user_filled_keys):
                        if field.get('saved_city_check'):result['saved_city_check']=field['saved_city_check']
                        if re.search(r'country|state',field['question'],re.I):result['control_choices']=[o['label'] for o in field.get('options',[])]
                        return needs(field['question'],'saved_answer_not_restored' if k in answered else 'confirmed_answer_not_accepted_by_control',field)
                    staged.append({'key':k,'question':field['question'],'fact_ref':fact})
                    if 'autocomplete_controls' in field:result['autocomplete_controls']=field['autocomplete_controls']
                    if 'autocomplete_input' in field:result['autocomplete_input']=field['autocomplete_input']
                uploads=modal.locator('input[type=file]')
                if uploads.count():
                    uploads.first.set_input_files(str(pdf));page.wait_for_timeout(1500)
                    resume_verified=pdf.name in modal.inner_text()
                    if not resume_verified:return needs('Verify the role-specific resume in this Easy Apply draft','resume_upload_not_verified')
                submit=modal.get_by_role('button',name=re.compile(r'^Submit application$',re.I))
                if submit.count()==1:
                    resume_verified=pdf.name in modal.inner_text()
                    if not resume_verified:return needs('Select and verify the role-specific PDF before submitting','resume_selection_unproven')
                    if not exact(page,req):return needs('Exact job identity changed before submission','job_identity_changed')
                    review=folder/'linkedin-easy-apply-presubmit.json'
                    review.write_text(json.dumps({'job_id':job['id'],'requisition_id':req,'provider':'linkedin_easy_apply',
                        'form_url':job['canonical_url'],'resume_sha256':ev['sha256'],'final_action':'submit_application',
                        'checks':{x:True for x in ('live','unapplied','fit','resume_visual','resume_selected','answers_confirmed','unique_apply')},
                        'fact_refs':[x['fact_ref'] for x in staged]},indent=2)+'\n')
                    form_sha=job_db.record_evidence(db,job['id'],'form_review',str(review.relative_to(ROOT)))
                    if job['state']=='ResumeReady':
                        job_db.transition(db,job['id'],'InProgress','linkedin_easy_apply','Verified exact Easy Apply review',str(review.relative_to(ROOT)),'ResumeReady')
                        job_db.transition(db,job['id'],'Preparing','linkedin_easy_apply','Confirmed fields and exact role PDF',str(review.relative_to(ROOT)),'InProgress')
                        job_db.transition(db,job['id'],'Prepared','linkedin_easy_apply','Final Submit identified',str(review.relative_to(ROOT)),'Preparing')
                    attempt=job_db.begin_attempt(db,job['id'],ev['path'],ev['sha256'],job['canonical_url'],
                        str(review.relative_to(ROOT)),form_sha,config=config,portal_worker='linkedin',bootstrap=bootstrap)
                    failed_submission_responses=[]
                    def response_check(response):
                        if re.search(r'onsiteApplyApplications|jobApplications|submitApplication',urlsplit(response.url).path,re.I) and response.status>=400:
                            failed_submission_responses.append(response.status)
                    page.on('response',response_check)
                    result['attempt_id']=attempt;submitted=True;persist();submit.click();page.wait_for_timeout(3500)
                    sent=page.get_by_text(re.compile(r'(?:Your application was sent|Application sent|Application submitted)',re.I))
                    if sent.count() and exact(page,req) and not failed_submission_responses:
                        stamp=job_db.now();proof=folder/'linkedin-easy-apply-confirmation.json'
                        proof.write_text(json.dumps({'attempt_id':attempt,'requisition_id':req,'captured_at':stamp,
                          'url':job['canonical_url'],'confirmation_text':'Application sent on the exact LinkedIn job flow',
                          'application_sent':True,'exact_job_id_match':True,'role_resume_sha256':ev['sha256']},indent=2)+'\n')
                        job_db.confirm_attempt(db,attempt,job['id'],str(proof.relative_to(ROOT)),stamp,config=config,portal_worker='linkedin',bootstrap=bootstrap)
                        result.update(status='Applied',confirmation_path=str(proof.relative_to(ROOT)))
                        progress.update(status='confirmed',pending_field=None,pending_reason=None,confirmation_path=str(proof.relative_to(ROOT)))
                        persist()
                        receipt=ROOT/'.workflow/portal-proof/linkedin.json';receipt.parent.mkdir(parents=True,exist_ok=True)
                        receipt.write_text(json.dumps({'status':'verified','provider':'linkedin','job_id':job['id'],
                            'requisition_id':req,'attempt_id':attempt,'proof_path':str(proof.relative_to(ROOT)),
                            'proof_sha256':sha(proof),'verified_at':stamp},indent=2)+'\n')
                    else:result.update(status='Uncertain',reason='final_submission_without_positive_exact_job_confirmation')
                    if any(status in (403,429) for status in failed_submission_responses):result['provider_stop_reason']='site_restriction_or_limit'
                    break
                next_button=modal.get_by_role('button',name=re.compile(r'^(?:Next|Review|Continue to next step|Review your application)(?:\s|$)',re.I))
                if next_button.count()!=1:
                    result['visible_buttons']=modal.locator('button:visible').evaluate_all("els=>els.map(e=>({text:e.innerText.trim(),label:e.getAttribute('aria-label'),disabled:e.disabled}))")
                    return needs('Inspect the same Easy Apply step','next_review_control_ambiguous')
                result['step_action']='next_or_review'
                next_button.click(timeout=8000);page.wait_for_timeout(1200)
                errors=modal.locator('.artdeco-inline-feedback--error:visible,[role=alert]:visible')
                if errors.count():
                    result['validation_errors']=errors.all_inner_texts()
                    result['invalid_controls']=modal.locator('[aria-invalid=true]').evaluate_all("els=>els.map(e=>({id:e.id,label:e.getAttribute('aria-label')||e.closest('.fb-dash-form-element')?.querySelector('label')?.innerText||'',type:e.type}))")
                    result['validation_context']=errors.evaluate_all("els=>els.map(e=>{const a=[];for(let p=e.parentElement,n=0;p&&n<4;p=p.parentElement,n++)a.push({tag:p.tagName,classes:p.className,labels:[...p.querySelectorAll('label')].map(l=>l.innerText.trim()),controls:[...p.querySelectorAll('input,select')].map(i=>({id:i.id,type:i.type}))});return a})")
                    return needs(staged[-1]['question'] if staged else 'Inspect validation errors','site_validation_error',fields[-1] if fields else None)
                for item in staged:
                    if item['fact_ref'].startswith('confirmed_identity:'):
                        progress['contact_verified']=[x for x in progress['contact_verified'] if x['key']!=item['key']]+[item]
                    else:answered[item['key']]=item
                progress['steps']+=1;persist()
            else:return needs('Inspect the saved Easy Apply draft','step_limit_reached')
    except Exception as exc:
        result.update(status='Uncertain' if attempt else 'Blocked',error_type=type(exc).__name__)
        frames=traceback.extract_tb(exc.__traceback__)
        frame=next((f for f in reversed(frames) if '/tools/' in f.filename),frames[-1])
        result['error_origin']={'file':frame.filename.rsplit('/',1)[-1],'line':frame.lineno,'function':frame.name}
    finally:
        result['submit_clicked']=submitted;out.write_text(json.dumps(result,indent=2)+'\n')
    return result

def run(db,config,*,test_mode=False,max_submissions=1,application_job_id=None):
    budget=AttemptBudget(max_submissions)
    if max_submissions>config['portal_workers']['linkedin']['max_submissions_per_run']:raise ValueError('LinkedIn attempt limit exceeds configured cap')
    if not test_mode and not job_config.live_provider_enabled(config,'linkedin'):return {'status':'DiscoveryOnly','submissions':0,'results':[]}
    bootstrap=bool(application_job_id)
    if not test_mode and not bootstrap and not job_db.portal_path_verified(db,'linkedin'):return {'status':'Blocked','submissions':0,'results':[],'blocker':'linkedin_live_proof_missing'}
    if application_job_id:
        row=db.execute("SELECT * FROM jobs WHERE id=? AND source='linkedin_portal_native' AND state='NeedsAnswer' AND NOT EXISTS(SELECT 1 FROM submission_attempts a WHERE a.job_id=jobs.id)",(application_job_id,)).fetchone()
        if row:
            path=ROOT/'jobs'/row['slug']/'linkedin-easy-apply-progress.json'
            saved=json.loads(path.read_text()) if path.exists() else {}
            control_fix=saved.get('pending_reason') in ('next_review_control_ambiguous','confirmed_answer_not_accepted_by_control','resume_upload_not_verified','resume_selection_unproven','site_validation_error','saved_answer_not_restored') or (saved.get('pending_reason')=='external_apply_or_unavailable' and saved.get('steps',0)>0)
            field=saved.get('pending_field')
            answerable=not field or forms.answer(field,(ROOT/'APPLICATION_PROFILE.md').read_text())[0] is not None
            if not saved.get('submit_clicked') and not saved.get('reconciliation_pending') and control_fix and answerable:
                job_db.transition(db,row['id'],'ResumeReady','linkedin_easy_apply','Controlled continuation of the same unsubmitted draft',str(path.relative_to(ROOT)),'NeedsAnswer')
                db.execute("UPDATE queue SET state='ResumeReady',retry_after=NULL WHERE job_id=?",(row['id'],))
    rows=candidates(db,config)
    if application_job_id is not None:rows=[x for x in rows if x['id']==application_job_id]
    results=[]
    provider_stop=False
    for row in rows[:max_submissions if test_mode else 30]:
        prior_claim=claim_id(db,row['id'])
        if not budget.can_start(prior_claim):break
        if results:pace()
        saved_path=ROOT/'jobs'/row['slug']/'linkedin-easy-apply-progress.json'
        saved=json.loads(saved_path.read_text()) if saved_path.exists() else {}
        existing_draft=bool(saved.get('draft_id') and saved.get('steps',0)>0 and not saved.get('submit_clicked'))
        try:result=_run_one(db,row,config,test_mode=test_mode,bootstrap=bootstrap)
        except ValueError as exc:
            result={'job_id':row['id'],'status':'NotReady','actual_submission':False} if test_mode else preflight_failure(row,exc,ROOT,'linkedin',None if prior_claim else claim_id(db,row['id']))
        results.append(result)
        if not test_mode:budget.record(result,prior_claim,claim_id(db,row['id']),existing_draft=existing_draft)
        if result['status']=='PausedOrDisabled' or result.get('provider_stop_reason') or result.get('reason')=='site_verification_or_limit':
            provider_stop=True;break
    return {'status':'Complete' if results else 'Idle','candidate_count':len(rows),'results':results,
            'submissions':sum(r['status']=='Applied' for r in results),'blocked_jobs':sum(r['status'] in ('NeedsAnswer','Blocked') for r in results),
            'provider_stopped':provider_stop,**budget.summary()}
