"""One entrypoint to the pipeline for the CLI (`apply-now`), the UI (Run Now) and any other trigger.

The systemd service reaches `pipeline_v2.run(scheduled=True)` through `workflow daily --scheduled`;
`execute()` calls the same function, so config, the run lock, pause control, recovery, fit rules,
resume checks, claims, proof gates and alerts are shared. Manual runs work while the timer is
switched off (`manual=True`), but never while the workflow is paused or another run holds the lock.
"""
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import control
import job_config
import pipeline_v2
from local_paths import ROOT

STATE=ROOT/'.workflow'
LOCK=STATE/'daily-run.lock'
UNIT='job-search-manual'
SERVICE_UNIT=Path.home()/'.config/systemd/user/job-search-daily.service'

def service_path():
    """PATH from the systemd unit (contains node/npx for the browser bridge)."""
    try:
        for line in SERVICE_UNIT.read_text().splitlines():
            if line.startswith('Environment=PATH='):return line.split('=',2)[2]
    except OSError:pass
    return os.environ.get('PATH','/usr/bin:/bin')

def ensure_service_path():
    if not shutil.which('npx'):os.environ['PATH']=service_path()

def lock_held():
    """True when another run owns the pipeline lock (probe, never blocks)."""
    STATE.mkdir(exist_ok=True)
    with LOCK.open('a') as handle:
        try:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);fcntl.flock(handle,fcntl.LOCK_UN);return False
        except BlockingIOError:return True

def can_start():
    """(ok, reason) without side effects."""
    if control.is_paused():return False,'The workflow is paused; resume it first'
    if lock_held():return False,'Another run is active'
    return True,''

def execute(*,dry_run=False,trigger='cli',progress=None,sources=None,review_limit=None,fresh_only=False,max_seconds=None,page_cap=None,query_cap=None,application_job_id=None,queue_only=False,session_check_only=False):
    """Run the shared pipeline in this process."""
    ensure_service_path()
    os.environ['JOB_SEARCH_TRIGGER']=trigger
    config=job_config.load()
    return pipeline_v2.run(test=dry_run,scheduled=True,manual=True,max_seconds=max_seconds or config['schedule']['max_seconds'],progress=progress,
        sources=sources or tuple(p for p in ('naukri','linkedin') if config['sources'][p]['enabled']),review_limit=review_limit,fresh_only=fresh_only,page_cap=page_cap or 1,query_cap=query_cap or 2,application_job_id=application_job_id,queue_only=queue_only,session_check_only=session_check_only)

def start_detached(*,dry_run=False,trigger='ui'):
    """Launch `workflow apply-now` outside the caller (UI): transient systemd user unit, else a detached process."""
    ok,reason=can_start()
    if not ok:return {'started':False,'reason':reason}
    argv=[sys.executable,str(ROOT/'workflow'),'apply-now']+(['--dry-run'] if dry_run else [])
    env={'JOB_SEARCH_TRIGGER':trigger}
    if shutil.which('systemd-run'):
        unit=f'{UNIT}-{int(time.time())}'
        cmd=['systemd-run','--user','--collect','--quiet',f'--unit={unit}',f'--working-directory={ROOT}',
             '-p','TimeoutStartSec=1830','-E','PATH='+service_path(),'-E','JOB_SEARCH_TRIGGER='+trigger,*argv]
        done=subprocess.run(cmd,capture_output=True,text=True)
        if done.returncode==0:return {'started':True,'via':'systemd-run','unit':unit,'dry_run':dry_run}
    process=subprocess.Popen(argv,cwd=ROOT,env={**os.environ,**env,'PATH':service_path()},start_new_session=True,
                             stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    return {'started':True,'via':'subprocess','pid':process.pid,'dry_run':dry_run}
