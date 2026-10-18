"""Independent provider budgets count new claims, including blocked/uncertain claims."""
import time
import json
from pathlib import Path

def preflight_failure(job,error,root,provider,attempt=None):
    from ui_api import scrub
    import re
    result={'job_id':job['id'],'status':'Uncertain' if attempt else 'Blocked','attempt_id':attempt,
            'stage':'worker_preflight','error_type':type(error).__name__,
            'error_message':scrub(re.sub(r'https?://\S+','[URL omitted]',str(error)))}
    folder=Path(root)/'jobs'/job['slug'];folder.mkdir(parents=True,exist_ok=True)
    name='naukri-native-outcome.json' if provider=='naukri' else 'linkedin-easy-apply-outcome.json'
    (folder/name).write_text(json.dumps(result,indent=2)+'\n')
    return result

def claim_id(db,job_id):
    row=db.execute('SELECT attempt_id FROM submission_attempts WHERE job_id=?',(job_id,)).fetchone()
    return row['attempt_id'] if row else None

class AttemptBudget:
    def __init__(self,limit):
        if type(limit) is not int or not 1<=limit<=10:raise ValueError('Provider new-attempt limit must be 1–10')
        self.limit=limit;self.used=0;self.new_claims=0;self.continuations=0;self.repairs=0

    def can_start(self,prior_claim):return bool(prior_claim) or self.used<self.limit

    def record(self,result,prior_claim,current_claim,existing_draft=False):
        if prior_claim and current_claim!=prior_claim:
            raise RuntimeError('An existing attempt must retain its original claim')
        repair_only=bool(result.get('preflight_resume_repair_only') or result.get('generic_resume_repaired_at_start')) and not prior_claim and not current_claim
        is_new=not prior_claim and bool(current_claim)
        self.used+=int(is_new);self.new_claims+=int(not prior_claim and bool(current_claim))
        continuation=not is_new and bool(prior_claim or existing_draft)
        self.continuations+=int(continuation);self.repairs+=int(repair_only)
        if self.used>self.limit:raise RuntimeError('Provider new-attempt budget exceeded')
        result.update(new_attempt_slot_consumed=is_new,new_claim_created=not prior_claim and bool(current_claim),existing_attempt_continued=continuation)

    def summary(self):
        return {'max_new_attempts_per_run':self.limit,'new_attempts_used':self.used,
                'new_claims_created':self.new_claims,'existing_attempts_continued':self.continuations,'preflight_repairs_without_claim':self.repairs}

def pace():
    # Additional pacing between jobs; existing page/control waits remain intact.
    time.sleep(2)
