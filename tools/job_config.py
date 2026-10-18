"""Strict allowlist and single human-editable configuration."""
import datetime as dt
import fcntl
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from local_paths import ROOT
import yaml

PATH=ROOT/'config/job_search.yaml'
PORTAL_HOSTS={'naukri':'www.naukri.com','linkedin':'www.linkedin.com',
              'foundit':'www.foundit.in','indeed':'in.indeed.com','cutshort':'cutshort.io'}
BOARD_TYPES={'lever','greenhouse','ashby'}
TOP={'version','mode','sources','employer_boards','search','high_fit','schedule',
     'review','concurrency','alerts','retention','submissions','naukri_worker',
     'direct_requisition_rules','freshness','portal_workers'}

def _timestamp(value,label):
    if value is None:return
    if not isinstance(value,str):raise ValueError(label+' must be ISO-8601 text or null')
    try:parsed=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:raise ValueError(label+' is not an ISO-8601 timestamp')
    if not parsed.tzinfo:raise ValueError(label+' needs a timezone')

def _positive(value,label,minimum=1,maximum=100):
    if type(value) is not int or not minimum<=value<=maximum:
        raise ValueError(f'{label} must be an integer from {minimum} to {maximum}')

PORTAL_LANES=('naukri','linkedin','foundit','indeed','cutshort','ats')

def _proof_marker(portal):
    return (ROOT/'.workflow/portal-proof'/f'{portal}.json').is_file()

def validate(data):
    if not isinstance(data,dict) or set(data)!=TOP:raise ValueError('Configuration keys differ from the required schema')
    if data['version']!=1 or data['mode'] not in ('read_only','production'):
        raise ValueError('Unsupported configuration version or mode')
    freshness=data['freshness']
    if not isinstance(freshness,dict) or set(freshness)!={'routine_review_hours','sweep_overlap_minutes',
            'unknown_date_details_per_run','transient_retry_hours','pending_application_recheck_hours'}:
        raise ValueError('Posting freshness policy incomplete')
    for name,maximum in (('routine_review_hours',24),('sweep_overlap_minutes',180),
            ('unknown_date_details_per_run',30),('transient_retry_hours',24),
            ('pending_application_recheck_hours',24)):
        _positive(freshness[name],'freshness.'+name,1,maximum)
    sources=data['sources']
    if not isinstance(sources,dict) or set(sources)!=set(PORTAL_HOSTS):
        raise ValueError('Portal allowlist must explicitly include the five supported sources')
    for name,record in sources.items():
        if set(record)!={'type','enabled','host','search_url','last_successful_fetch','max_queries','max_pages'}:
            raise ValueError(f'Invalid source fields: {name}')
        if record['type']!='portal' or type(record['enabled']) is not bool or record['host']!=PORTAL_HOSTS[name]:
            raise ValueError(f'Invalid portal allowlist entry: {name}')
        template=record['search_url']
        if not isinstance(template,str) or not all('{'+k+'}' in template for k in ('query','location')):
            raise ValueError(f'Source URL needs query/location placeholders: {name}')
        try:url=template.format(query='java',location='India',page=1,offset=0)
        except KeyError:raise ValueError(f'Unexpected URL placeholder: {name}')
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname!=record['host'] or parsed.username or parsed.password or parsed.fragment:
            raise ValueError(f'Portal URL escapes the allowlist: {name}')
        _timestamp(record['last_successful_fetch'],name+'.last_successful_fetch')
        _positive(record['max_queries'],name+'.max_queries',1,8)
        _positive(record['max_pages'],name+'.max_pages',1,5)
    boards=data['employer_boards']
    if not isinstance(boards,dict):raise ValueError('Employer boards must be a mapping')
    for name,record in boards.items():
        if not re.fullmatch(r'[a-z][a-z0-9_]{2,50}',name):raise ValueError('Unsafe board name')
        if set(record)!={'type','enabled','employer','board','last_successful_fetch','max_pages','page_size'}:
            raise ValueError(f'Invalid employer board fields: {name}')
        if record['type'] not in BOARD_TYPES or type(record['enabled']) is not bool:
            raise ValueError(f'Unsupported employer board: {name}')
        if not isinstance(record['employer'],str) or not record['employer'].strip():raise ValueError('Employer name required')
        if not re.fullmatch(r'[A-Za-z0-9_-]{2,70}',str(record['board'])):raise ValueError('Unsafe public board token')
        _timestamp(record['last_successful_fetch'],name+'.last_successful_fetch')
        _positive(record['max_pages'],name+'.max_pages',1,5)
        _positive(record['page_size'],name+'.page_size',1,100)
    rules=data['direct_requisition_rules']
    rule_hosts={'lever':['jobs.lever.co'],
                'greenhouse':['job-boards.greenhouse.io','boards.greenhouse.io'],
                'ashby':['jobs.ashbyhq.com']}
    if not isinstance(rules,dict) or set(rules)!=set(rule_hosts)|{'employer_site'}:
        raise ValueError('Direct requisition rules must explicitly include the supported ATS and employer-page policies')
    for provider in rule_hosts:
        record=rules[provider]
        if set(record)!={'enabled','hosts','path_pattern'} or type(record['enabled']) is not bool:
            raise ValueError('Invalid direct requisition rule: '+provider)
        if record['hosts']!=rule_hosts[provider]:
            raise ValueError('Direct ATS hosts must match the fixed provider allowlist: '+provider)
        if not isinstance(record['path_pattern'],str) or len(record['path_pattern'])>180:
            raise ValueError('Invalid ATS requisition path rule: '+provider)
        try:re.compile(record['path_pattern'],re.I)
        except re.error as exc:raise ValueError('Invalid ATS path pattern: '+provider) from exc
    employer_rule=rules['employer_site']
    if set(employer_rule)!={'enabled','host_policy','apply_link_text_pattern','path_pattern','max_links_per_lead'} or type(employer_rule['enabled']) is not bool:
        raise ValueError('Invalid linked employer-page rule')
    if employer_rule['host_policy']!='linked_apply_url_exact_host':
        raise ValueError('Employer pages may only be reached through an exact portal Apply link')
    for key in ('apply_link_text_pattern','path_pattern'):
        if not isinstance(employer_rule[key],str) or len(employer_rule[key])>200:raise ValueError('Invalid employer-page matching rule')
        try:re.compile(employer_rule[key],re.I)
        except re.error as exc:raise ValueError('Invalid employer-page regular expression') from exc
    _positive(employer_rule['max_links_per_lead'],'direct_requisition_rules.employer_site.max_links_per_lead',1,3)
    if not any(rule['enabled'] for rule in rules.values()) and not any(data['sources'][p]['enabled'] for p in ('naukri','linkedin')):
        raise ValueError('At least one direct ATS provider rule must be enabled')
    search=data['search']
    if set(search)!={'terms','target_roles','locations','experience_years','hard_exclusions'}:
        raise ValueError('Search settings incomplete')
    for field in ('terms','target_roles','locations','hard_exclusions'):
        if not isinstance(search[field],list) or not search[field] or any(not isinstance(x,str) or not x.strip() for x in search[field]):
            raise ValueError('Invalid search list: '+field)
    exp=search['experience_years']
    if set(exp)!={'min','max'} or not 0<=exp['min']<=exp['max']<=20:raise ValueError('Invalid experience range')
    fit=data['high_fit']
    old_fit={'minimum_essential_coverage','maximum_required_professional_years','require_java_and_spring_boot','require_substantial_backend','require_india_location','allow_project_training_evidence_with_depth_review'}
    if set(fit) not in (old_fit,old_fit|{'secondary_skills_policy','maximum_approved_near_miss_years'}):
        raise ValueError('High-fit gates incomplete')
    if 'secondary_skills_policy' in fit and (fit['secondary_skills_policy'] not in ('block','interview_preparation') or fit['maximum_approved_near_miss_years']!=3):raise ValueError('Unsupported suitability policy or near-miss experience')
    if fit['minimum_essential_coverage']!=1.0 or not 0<fit['maximum_required_professional_years']<=3:
        raise ValueError('High fit cannot relax essential coverage or experience gate')
    if not all(fit[k] is True for k in ('require_java_and_spring_boot','require_substantial_backend','require_india_location','allow_project_training_evidence_with_depth_review')):
        raise ValueError('High-fit evidence gates must remain enabled')
    review=data['review']
    if set(review)!={'per_company_cap','max_jds_per_run','max_original_checks_per_company','max_portal_leads_per_run'}:
        raise ValueError('Review bounds incomplete')
    for name,maximum in (('per_company_cap',10),('max_jds_per_run',50),('max_original_checks_per_company',50),('max_portal_leads_per_run',30)):
        _positive(review[name],'review.'+name,1,maximum)
    if review['per_company_cap']>review['max_jds_per_run']:
        raise ValueError('Per-company review cap exceeds run limit')
    schedule=data['schedule']
    if set(schedule)!={'enabled','application_enabled','timezone','times','missed_run_catchup','max_seconds'} or schedule['timezone']!='Asia/Kolkata' or schedule['times']!=['08:30','12:30','16:30','20:30'] or schedule['missed_run_catchup'] is not False:
        raise ValueError('Four IST slots and missed-run behavior must be explicit')
    if type(schedule['enabled']) is not bool:_positive(None,'schedule.enabled')
    if type(schedule['application_enabled']) is not bool:
        raise ValueError('schedule.application_enabled must be explicit')
    _positive(schedule['max_seconds'],'schedule.max_seconds',60,1800)
    concurrency=data['concurrency']
    if set(concurrency)!={'discovery','verification','resume','application','per_employer','per_ats'}:
        raise ValueError('Concurrency settings incomplete')
    for k,v in concurrency.items():_positive(v,'concurrency.'+k,1,3)
    if concurrency['per_employer']!=1 or concurrency['per_ats']!=1:
        raise ValueError('Employer/ATS interactions must remain serial')
    alerts=data['alerts']
    if set(alerts)!={'desktop_enabled','phone_enabled','repeat_cooldown_minutes'} or type(alerts['desktop_enabled']) is not bool or type(alerts['phone_enabled']) is not bool:
        raise ValueError('Invalid alert settings')
    _positive(alerts['repeat_cooldown_minutes'],'alerts.repeat_cooldown_minutes',1,1440)
    retention=data['retention']
    if set(retention)!={'terminal_details_days','submission_proof_days','run_logs_days'}:raise ValueError('Retention settings incomplete')
    _positive(retention['terminal_details_days'],'retention.terminal_details_days',0,365)
    _positive(retention['submission_proof_days'],'retention.submission_proof_days',30,365)
    _positive(retention['run_logs_days'],'retention.run_logs_days',1,365)
    submissions=data['submissions']
    if set(submissions)!={'enabled','require_live_form_test','require_confirmation_proof'} or any(type(submissions[k]) is not bool for k in submissions):
        raise ValueError('Invalid submission settings')
    if data['mode']=='read_only' and submissions['enabled']:
        raise ValueError('Read-only mode cannot enable submission')
    if not submissions['require_live_form_test'] or not submissions['require_confirmation_proof']:
        raise ValueError('Submission proof gates cannot be disabled')
    if concurrency['application']>1:
        raise ValueError('Parallel form attachment isolation has not passed; application cap is one')
    worker=data['naukri_worker']
    if (not isinstance(worker,dict) or set(worker)!={'enabled','max_submissions_per_run',
            'medium_max_required_years_exclusive','excluded_requisition_ids',
            'require_role_resume_visual_review','require_generic_resume_restore'}
            or type(worker['enabled']) is not bool or type(worker['max_submissions_per_run']) is not int or not 1<=worker['max_submissions_per_run']<=10
            or worker['medium_max_required_years_exclusive']!=4
            or not isinstance(worker['excluded_requisition_ids'],list)
            or any(not re.fullmatch(r'\d{7,20}',str(req)) for req in worker['excluded_requisition_ids'])
            or worker['require_role_resume_visual_review'] is not True
            or worker['require_generic_resume_restore'] is not True):
        raise ValueError('Naukri worker must retain bounded attempts, experience, resume-review and restore guards')
    lanes=data['portal_workers']
    if not isinstance(lanes,dict) or set(lanes)!=set(PORTAL_LANES):
        raise ValueError('portal_workers must list exactly: '+', '.join(PORTAL_LANES))
    for name,record in lanes.items():
        if (not isinstance(record,dict) or set(record) not in ({'apply_enabled'},{'apply_enabled','scheduled_apply_enabled','max_submissions_per_run'},{'apply_enabled','scheduled_apply_enabled','max_submissions_per_run','max_new_attempts_per_run'})
                or type(record['apply_enabled']) is not bool):
            raise ValueError('Invalid portal worker entry: '+name)
        if 'scheduled_apply_enabled' in record:
            if type(record['scheduled_apply_enabled']) is not bool:raise ValueError('Scheduled provider flag must be boolean')
            _positive(record['max_submissions_per_run'],'portal_workers.'+name+'.max_submissions_per_run',1,10)
            if 'max_new_attempts_per_run' in record:
                _positive(record['max_new_attempts_per_run'],'portal_workers.'+name+'.max_new_attempts_per_run',1,10)
                if record['max_new_attempts_per_run']!=record['max_submissions_per_run']:raise ValueError('Provider attempt limit and legacy alias must agree')
            if record['scheduled_apply_enabled'] and not record['apply_enabled']:raise ValueError('Scheduled lane requires its live flag')
            if name=='linkedin' and record['scheduled_apply_enabled'] and not _proof_marker(name):raise ValueError('LinkedIn timer requires actual Easy Apply proof')
        if record['apply_enabled'] and name not in ('naukri','linkedin'):raise ValueError('Only Naukri and LinkedIn support applications')
    if submissions['enabled'] and not any(live_provider_enabled(data,p) for p in ('naukri','linkedin')):
        raise ValueError('Production applications require an enabled provider lane')
    return data

def live_naukri_enabled(data, *, test_mode=False, timer=False):
    """One execution gate for the CLI, timer, UI, worker and database claims."""
    return bool(not test_mode and data['mode']=='production' and data['submissions']['enabled']
        and data['sources']['naukri']['enabled'] and data['naukri_worker']['enabled']
        and data['portal_workers']['naukri']['apply_enabled']
        and (not timer or data['schedule']['application_enabled'] and data['portal_workers']['naukri'].get('scheduled_apply_enabled',True)))

def live_provider_enabled(data,provider,*,test_mode=False,timer=False):
    if provider=='naukri':return live_naukri_enabled(data,test_mode=test_mode,timer=timer)
    return bool(provider=='linkedin' and not test_mode and data['mode']=='production' and data['submissions']['enabled']
        and data['sources'][provider]['enabled'] and data['portal_workers'][provider]['apply_enabled']
        and (not timer or data['schedule']['application_enabled'] and data['portal_workers'][provider].get('scheduled_apply_enabled',False)))

def load(path=PATH):
    data=yaml.safe_load(Path(path).read_text())
    return validate(data)

def source_rows(data):
    rows=[{'name':name,'kind':record['type'],'enabled':record['enabled'],
             'last_successful_fetch':record['last_successful_fetch']}
            for section in ('sources','employer_boards') for name,record in data[section].items()]
    rows.extend({'name':name,'kind':'direct_'+name,'enabled':record['enabled'],
                 'last_successful_fetch':None} for name,record in data['direct_requisition_rules'].items())
    return rows

def note_success(section,name,at,path=PATH):
    if section not in ('sources','employer_boards'):raise ValueError('Unknown source section')
    _timestamp(at,'fetch time')
    path=Path(path);lock=path.with_suffix('.lock')
    with lock.open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        data=load(path)
        if name not in data[section] or not data[section][name]['enabled']:
            raise ValueError('Disabled or unlisted source cannot record success')
        data[section][name]['last_successful_fetch']=at
        descriptor,tmp=tempfile.mkstemp(dir=path.parent,prefix='.config-',suffix='.yaml')
        try:
            with os.fdopen(descriptor,'w') as file:yaml.safe_dump(data,file,sort_keys=False,allow_unicode=True)
            os.replace(tmp,path)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)


def _rewrite(mutator,path=PATH):
    path=Path(path);lock=path.with_suffix('.lock')
    with lock.open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        data=load(path);mutator(data);validate(data)
        descriptor,tmp=tempfile.mkstemp(dir=path.parent,prefix='.config-',suffix='.yaml')
        try:
            with os.fdopen(descriptor,'w') as file:yaml.safe_dump(data,file,sort_keys=False,allow_unicode=True)
            os.chmod(tmp,0o600);os.replace(tmp,path)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
    return data

def set_source_enabled(section,name,enabled,path=PATH):
    """Toggle discovery for one configured source; the strict validator still applies."""
    def change(data):
        if section not in ('sources','employer_boards') or name not in data[section]:raise ValueError('Unknown source')
        data[section][name]['enabled']=bool(enabled)
    return _rewrite(change,path)

def set_apply_enabled(portal,enabled,path=PATH):
    def change(data):
        if portal not in data['portal_workers']:raise ValueError('Unknown portal')
        data['portal_workers'][portal]['apply_enabled']=bool(enabled)
    return _rewrite(change,path)

def set_naukri_mode(live,scheduled=False,path=PATH):
    if live:
        import job_db
        db=job_db.connect()
        try:
            if not job_db.portal_path_verified(db,'naukri'):raise ValueError('Job-bound Naukri application proof is missing')
        finally:db.close()
    if scheduled:
        import json
        receipt=ROOT/'.workflow/naukri-live-verified.json'
        try:proof=json.loads(receipt.read_text())
        except (OSError,ValueError):raise ValueError('Confirm a live application and generic resume restoration before enabling scheduled applications')
        if not proof.get('application_confirmed') or not proof.get('generic_resume_restored'):
            raise ValueError('Live application and restore proof is incomplete')
        import hashlib
        db=job_db.connect()
        try:
            attempt=db.execute('SELECT * FROM submission_attempts WHERE attempt_id=?',(proof.get('attempt_id'),)).fetchone()
            if (not attempt or attempt['state']!='confirmed' or attempt['job_id']!=proof.get('job_id')
                    or attempt['proof_path']!=proof.get('proof_path') or attempt['proof_sha256']!=proof.get('proof_sha256')):
                raise ValueError('Scheduled proof must match a confirmed workflow attempt')
            artifact=(ROOT/attempt['proof_path']).resolve()
            if not artifact.is_relative_to(ROOT/'jobs') or hashlib.sha256(artifact.read_bytes()).hexdigest()!=attempt['proof_sha256']:
                raise ValueError('Scheduled application proof changed')
            if hashlib.sha256((ROOT/'master-resume/Yugal_Teotia_CV.pdf').read_bytes()).hexdigest()!=proof.get('generic_resume_sha256'):
                raise ValueError('Scheduled restore proof does not match the current master PDF')
        finally:db.close()
    def change(data):
        data['mode']='production' if live else 'read_only';data['submissions']['enabled']=bool(live)
        data['naukri_worker']['enabled']=True;data['portal_workers']['naukri']['apply_enabled']=bool(live)
        if 'scheduled_apply_enabled' in data['portal_workers']['naukri']:
            data['portal_workers']['naukri']['scheduled_apply_enabled']=bool(live and scheduled)
        if not live:
            data['portal_workers']['linkedin']['apply_enabled']=False
            if 'scheduled_apply_enabled' in data['portal_workers']['linkedin']:data['portal_workers']['linkedin']['scheduled_apply_enabled']=False
        data['schedule']['application_enabled']=any(data['portal_workers'][p].get('scheduled_apply_enabled',False) for p in ('naukri','linkedin'))
    result=_rewrite(change,path)
    if Path(path)==PATH:
        import control
        control.sync(result)
    return result

def set_linkedin_mode(live,scheduled=False,path=PATH):
    if scheduled:
        import job_db
        db=job_db.connect()
        try:
            if not job_db.portal_path_verified(db,'linkedin'):raise ValueError('A real confirmed LinkedIn Easy Apply proof is required for its timer lane')
        finally:db.close()
    def change(data):
        data['portal_workers']['linkedin'].update(apply_enabled=bool(live),scheduled_apply_enabled=bool(live and scheduled))
        any_live=any(data['portal_workers'][p]['apply_enabled'] for p in ('naukri','linkedin'))
        data['mode']='production' if any_live else 'read_only';data['submissions']['enabled']=any_live
        data['schedule']['application_enabled']=any(data['portal_workers'][p].get('scheduled_apply_enabled',False) for p in ('naukri','linkedin'))
    result=_rewrite(change,path)
    if Path(path)==PATH:
        import control
        control.sync(result)
    return result
