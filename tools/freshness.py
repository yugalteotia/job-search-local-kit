"""Posting-time priority and pre-detail repeat suppression."""
import datetime as dt
import hashlib
import json
from urllib.parse import urlsplit

REVIEWED={'PortalNativeVerified','PortalNativeUnverified','OriginalMatched','NoAllowlistedOriginal',
          'DirectJDUnavailable','PortalDetailsUnavailable','PortalNativeJDUnavailable'}
PENDING_APPLICATION={'Shortlisted','ResumeReady','Preparing','Prepared','NeedsAnswer','SubmissionUnconfirmed'}

def precise(value):
    """Accept an explicit clock time and offset, never a calendar-only date."""
    if not isinstance(value,str) or 'T' not in value or ':' not in value:return None
    try:parsed=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:return None
    return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo else None

def classify(posted_at,last_success,policy,now=None):
    now=now or dt.datetime.now(dt.timezone.utc)
    posted=precise(posted_at)
    if posted is None:return 'Unknown','posting_clock_time_not_stated'
    age=(now-posted).total_seconds()/3600
    if age<-.0834:return 'Unknown','posting_timestamp_is_in_future'
    if age>policy['routine_review_hours']:return 'Old','posting_older_than_routine_review_window'
    last=precise(last_success)
    if last and posted<last-dt.timedelta(minutes=policy['sweep_overlap_minutes']):
        return 'Recent','posted_before_last_sweep_overlap_but_within_24_hours'
    return 'Fresh','posted_since_last_successful_sweep_overlap'

def fingerprint(source,lead):
    stable={key:lead.get(key) for key in ('portal_job_id','url','title','employer','card_location','posted_at','posted_text','card_excerpt','card_skills')}
    stable['source']=source
    return hashlib.sha256(json.dumps(stable,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def _checked_age_hours(value,now):
    parsed=precise(value)
    return (now-parsed).total_seconds()/3600 if parsed else float('inf')

def select(db,leads,config,*,now=None):
    """Return eligible fresh/recent/unknown leads and explicit exclusion counts."""
    now=now or dt.datetime.now(dt.timezone.utc)
    policy=config['freshness'];kept=[];counts={'fresh':0,'recent':0,'unknown_date':0,
        'skipped_old':0,'repeat_skipped':0,'history_skipped':0,'unknown_date_deferred':0}
    reasons=[];seen=set();unknown=0
    for source,lead in leads:
        url=lead.get('url');identity=(source,str(lead.get('portal_job_id') or url))
        if not url or identity in seen:
            counts['repeat_skipped']+=1;reasons.append({'source':source,'url':url,'reason':'duplicate_in_same_sweep'});continue
        seen.add(identity)
        if source=='naukri' and str(lead.get('portal_job_id')) in {str(x) for x in config['naukri_worker']['excluded_requisition_ids']}:
            counts['history_skipped']+=1;reasons.append({'source':source,'url':url,'reason':'protected_requisition'});continue
        if db.execute('SELECT 1 FROM history_keys WHERE direct_link=?',(url,)).fetchone():
            counts['history_skipped']+=1;reasons.append({'source':source,'url':url,'reason':'terminal_history_key'});continue
        job=db.execute('SELECT state,fit,checked_at FROM jobs WHERE canonical_url=?',(url,)).fetchone()
        if job and job['state']=='SubmissionUnconfirmed':
            counts['history_skipped']+=1;reasons.append({'source':source,'url':url,'reason':'locked_uncertain_attempt'});continue
        if job and job['state'] in ('Applied','Rejected','Closed','Expired','Withdrawn'):
            counts['history_skipped']+=1;reasons.append({'source':source,'url':url,'reason':'existing_terminal_or_applied_job'});continue
        category,reason=classify(lead.get('posted_at'),config['sources'][source]['last_successful_fetch'],policy,now)
        prior=db.execute('SELECT status,checked_at,result_json FROM lead_checks WHERE canonical_url=?',(url,)).fetchone()
        card_hash=fingerprint(source,lead)
        if prior:
            try:old=json.loads(prior['result_json'] or '{}')
            except ValueError:old={}
            unchanged=old.get('card_sha256')==card_hash
            pending=job and job['state'] in PENDING_APPLICATION
            age=_checked_age_hours(prior['checked_at'],now)
            recheck_hours=policy['pending_application_recheck_hours'] if pending else policy['routine_review_hours']
            if unchanged and prior['status'] in REVIEWED and age<recheck_hours:
                counts['repeat_skipped']+=1;reasons.append({'source':source,'url':url,'reason':'unchanged_already_reviewed'});continue
            if unchanged and prior['status'] not in REVIEWED and age<policy['transient_retry_hours']:
                counts['repeat_skipped']+=1;reasons.append({'source':source,'url':url,'reason':'site_failure_retry_cooldown'});continue
        if category=='Old' and not (job and job['state'] in PENDING_APPLICATION):
            counts['skipped_old']+=1;reasons.append({'source':source,'url':url,'reason':reason});continue
        if category=='Unknown':
            if unknown>=policy['unknown_date_details_per_run']:
                counts['unknown_date_deferred']+=1;reasons.append({'source':source,'url':url,'reason':'unknown_date_detail_cap'});continue
            unknown+=1;counts['unknown_date']+=1
        else:counts[category.lower()]+=1
        kept.append((source,{**lead,'freshness':category,'freshness_reason':reason,'card_sha256':card_hash}))
    # Exact post times since the last sweep first, then recent, then unknown.
    rank={'Fresh':0,'Recent':1,'Unknown':2}
    kept.sort(key=lambda item:(rank[item[1]['freshness']],item[1].get('posted_at') or ''),reverse=False)
    return kept,counts,reasons
