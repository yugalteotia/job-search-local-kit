"""Real native workers through the shared scheduled pipeline, using isolated synthetic jobs/browser fixtures."""
import contextlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import auto_fit,auto_resume,job_db,job_config,pipeline_v2,progress
import linkedin_easy_apply as linked
from test_questionnaire_loop import LoopBase,Env,Chat,PROFILE
from test_auto_pipeline import NEWGEN,record

class Wizard:
    def __init__(self,pdf,req):
        self.step=0;self.values={};self.uploads=[];self.submits=0;self.pdf=pdf;self.req=req
        self.page=MagicMock();self.page.url='https://www.linkedin.com/jobs/view/'+req+'/'
        self.page.evaluate.side_effect=self.evaluate
        self.page.locator.side_effect=self.locator
        self.page.get_by_text.side_effect=lambda rx:self.count(1 if self.step==3 and 'application' in rx.pattern.lower() else 0)
        self.page.goto.side_effect=lambda url,**kw:setattr(self.page,'url',url)
        self.modal=MagicMock();self.modal.last=self.modal;self.modal.is_visible.return_value=True
        self.modal.locator.side_effect=self.modal_locator
        self.modal.inner_text.return_value=pdf.name
        self.modal.get_by_role.side_effect=self.button
    def count(self,n):
        x=MagicMock();x.count.return_value=n;return x
    def evaluate(self,script,*args):
        if args:
            return [{'index':0,'question':('What is your notice period?' if self.step==0 else 'What is your current CTC?'),
                     'type':'text','required':True,'options':[]}]
        return False
    def locator(self,selector):
        if selector==linked.MODAL:return self.modal
        x=self.count(1);x.filter.return_value=x;x.inner_text.return_value='Easy Apply';return x
    def modal_locator(self,selector):
        if selector=='input[type=file]':
            x=self.count(1 if self.step==0 else 0);x.first=x
            x.set_input_files.side_effect=lambda p:self.uploads.append(p);return x
        if 'error' in selector or 'alert' in selector:return self.count(0)
        x=MagicMock();x.fill.side_effect=lambda value:self.values.update({self.step:value})
        x.input_value.side_effect=lambda:self.values.get(self.step,'');return x
    def button(self,role,name):
        final='Submit application' in name.pattern
        x=self.count(1 if (final and self.step==2) or (not final and self.step<2) else 0)
        def click(**kw):
            if final:self.submits+=1
            if self.step==1:self.values[2]=self.values.get(1,'')
            self.step+=1
        x.click.side_effect=click;return x
    @contextlib.contextmanager
    def session(self,**kw):
        c=MagicMock();c.pages=[self.page];yield c

class ScheduledWorkers(LoopBase):
    def test_distinct_native_jobs_complete_through_shared_scheduled_pipeline(self):
        r=record(NEWGEN,portal='4470000001',employer='Synthetic LinkedIn')
        r.update(source='linkedin_portal_native',canonical_url='https://www.linkedin.com/jobs/view/4470000001/')
        r['provenance']['portal']='linkedin'
        li,_=job_db.create_job(self.db,r);job_db.transition(self.db,li,'Verified','fixture','synthetic',None,'Discovered')
        self.config['portal_workers']['linkedin'].update(apply_enabled=True,scheduled_apply_enabled=True)
        self.config['portal_workers']['naukri']['scheduled_apply_enabled']=True
        self.config['schedule']['enabled']=True
        self.config['schedule']['application_enabled']=True
        (self.tmp/'APPLICATION_PROFILE.md').write_text(PROFILE)
        state=self.tmp/'.workflow';(state/'runs').mkdir(parents=True,exist_ok=True)
        with patch.object(auto_resume,'ROOT',self.tmp),patch.object(job_db,'ROOT',self.tmp):
            auto_fit.run(self.db,self.config);auto_resume.run(self.db,self.config)
        folder=self.tmp/'jobs'/self.db.execute('select slug from jobs where id=?',(li,)).fetchone()[0]
        pdf=self.tmp/self.db.execute("select path from evidence where job_id=? and kind='resume_review'",(li,)).fetchone()[0]
        wizard=Wizard(pdf,'4470000001');env=Env(Chat([('What is your notice period?','text')]))
        original=self.run_one
        def naukri_run(db,job,config,answer_profile,test_mode,resume=None):
            return original(env,resume=resume)
        import naukri_native_worker as nw,urgent_alerts,job_views
        # Pipeline opens a separate connection to the same temporary SQLite database.
        connect=job_db.connect
        with contextlib.ExitStack() as s:
            for p in [patch.object(pipeline_v2,'ROOT',self.tmp),patch.object(pipeline_v2,'STATE',state),
                patch.object(progress,'STATE',state),patch.object(job_db,'ROOT',self.tmp),patch.object(job_db,'STATE',state),
                patch.object(job_db,'connect',side_effect=lambda:connect(self.tmp/'jobs.sqlite3')),
                patch.object(job_config,'load',return_value=self.config),patch.object(job_db,'portal_path_verified',return_value=True),
                patch.object(job_db,'naukri_worker_path_verified',return_value=True),patch('control.is_paused',return_value=False),
                patch.object(pipeline_v2,'_run_source',side_effect=lambda section,name,*a:(section,{'source':name,'status':'Success','checked_at':job_db.now(),'pages':[],'leads':[]})),
                patch.object(urgent_alerts,'sync_sqlite_blockers',return_value=[]),patch.object(job_views,'regenerate',return_value={}),
                patch.object(auto_fit,'run',return_value={}),patch.object(auto_resume,'run',return_value={}),
                patch.object(nw,'ROOT',self.tmp),patch.object(nw,'recover_pending_restore',return_value={'status':'NotNeeded'}),
                patch.object(nw,'_run_one',side_effect=naukri_run),patch.object(linked,'ROOT',self.tmp),
                patch.object(linked,'chrome',wizard.session),patch.object(linked,'status',return_value={'signed_in':True})]:s.enter_context(p)
            # Restore the actual inner Naukri function while its fixture is invoked.
            actual=nw._run_one
            def invoke(db,job,config,answer_profile,test_mode,resume=None):
                with patch.object(nw,'_run_one',actual._mock_wraps or self.original_naukri):
                    return original(env,resume=resume)
            # The outer dispatch is real; only its browser is simulated by LoopBase.
            nw._run_one.side_effect=invoke
            outcome=pipeline_v2.run(scheduled=True,sources=('naukri','linkedin'),review_limit=2,max_seconds=60)
        self.assertEqual(outcome['naukri_application_worker']['submissions'],1,outcome['naukri_application_worker'])
        self.assertEqual(outcome['linkedin_application_worker']['submissions'],1,outcome['linkedin_application_worker'])
        self.assertEqual(wizard.submits,1);self.assertEqual(len(wizard.uploads),1)
        self.assertEqual(self.db.execute('select count(*) from submission_attempts where state="confirmed"').fetchone()[0],2)
    def setUp(self):
        import naukri_native_worker
        self.original_naukri=naukri_native_worker._run_one
        super().setUp()

if __name__=='__main__':
    import unittest;unittest.main()
