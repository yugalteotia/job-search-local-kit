"""Bounded private-MCP CLI runs and a single opt-in systemd user timer."""
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import time
from zoneinfo import ZoneInfo
from local_paths import ROOT

STATE=ROOT/'.workflow'
UNIT='job-search-daily'
IST=ZoneInfo('Asia/Kolkata')
SCHEDULE_HOURS=(8,12,16,20)
SCHEDULE_MINUTE=30
MISSED_WINDOW_MINUTES=15

def schedule_window(current):
    local=current.astimezone(IST)
    return local.hour in SCHEDULE_HOURS and SCHEDULE_MINUTE<=local.minute<SCHEDULE_MINUTE+MISSED_WINDOW_MINUTES

def next_slots(current=None):
    local=(current or dt.datetime.now(IST)).astimezone(IST)
    result=[]
    for hour in SCHEDULE_HOURS:
        candidate=local.replace(hour=hour,minute=SCHEDULE_MINUTE,second=0,microsecond=0)
        if candidate<=local: candidate+=dt.timedelta(days=1)
        result.append(candidate)
    return sorted(result)

def gate(w):
    p=w.read_json(STATE/'browser-probe-private.json',{})
    if not (p.get('passed') is True and p.get('private_token_transport') is True):
        raise ValueError('Separate private no-click signed-in browser probe has not passed; schedule remains disabled.')
    from private_playwright import load_private
    load_private()  # Validate permissions without exposing credentials.

def cli_command():
    return [shutil.which('codex') or 'codex','exec','--ephemeral','--skip-git-repo-check',
            '--approve-for-me','--json','-C',str(ROOT),
            '-c','mcp_servers.playwright.command="python3"',
            '-c','mcp_servers.playwright.args='+json.dumps([str(ROOT/'tools/private_playwright.py')]),'-']

def stop_child(child):
    if child.poll() is None:
        os.killpg(child.pid,signal.SIGTERM)
        try: child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid,signal.SIGKILL);child.wait()

def run(w,test=False,scheduled=False,max_seconds=1800,stage=None):
    if not 1<=max_seconds<=1800:
        raise ValueError('Run budget must be between 1 and 1800 seconds')
    gate(w);w.check_pause()
    if scheduled:
        current=dt.datetime.now(IST)
        if not schedule_window(current):
            w.event('daily_skipped',reason='Outside 08:30, 12:30, 16:30, 20:30 IST fifteen-minute windows; no sleep catch-up')
            return {'status':'Skipped outside scheduled window'}
    if not test and not stage:
        import terminal_cleanup
        terminal_cleanup.after_normal_run(w)
    if subprocess.run(['pgrep','-u',str(os.getuid()),'-x','chrome|chromium|google-chrome|chromium-browser'],capture_output=True).returncode:
        w.event('daily_blocked',reason='Chrome is closed; no browser run launched')
        result={'status':'Blocked','reason':'Chrome is closed; leave the signed-in Chrome profile open.','finished_at':w.now()}
        w.write_json(STATE/'last-daily-run.json',result)
        return result
    if not test and not stage and w.read_json(STATE/'parallel-config.json',{}).get('enabled') is True:
        import parallel_runner,terminal_cleanup
        try:return parallel_runner.run(w,max_seconds)
        finally:terminal_cleanup.after_normal_run(w)
    STATE.mkdir(exist_ok=True)
    with (STATE/'run.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('A run is already active')
        stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')
        log=STATE/'runs'/f'{stamp}-daily.jsonl'
        log.parent.mkdir(exist_ok=True)
        contract=ROOT/'DAILY_WORKFLOW_AGENT.md'
        prompt=(contract if contract.exists() else ROOT/'WORKFLOW_AGENT.md').read_text()
        prompt+=f'\nRun ID: {stamp}. Wall-clock budget {max_seconds} seconds.\n'
        prompt+='''\nTAB OWNERSHIP REQUIREMENT: On `browser_tabs` action `new`, immediately record the exact numeric tab ID shown in that response for this MCP connection. Reuse only IDs created by this workflow, with at most three worker tabs. At worker/run end close only those recorded IDs using `browser_tabs` close with the recorded ID. Never close a pre-existing tab, or infer ownership from URL, title, or Playwright group. If an ID is missing or uncertain, leave that tab open and report it. Include a `Tab lifecycle` JSON object in the digest with created, reused, closed, remaining_automation_tabs, and uncertain_ids.\n'''
        if stage:
            prompt+=f'\nRun only stage: {stage}. Submission remains disabled for this explicitly scoped stage run. Stop before Submit.\n'
        if test:
            prompt+='\nBOUNDED SCHEDULE TEST: browser read-only. Do not fill, upload, submit, send mail or change account settings. Check Naukri search once with newest sorting if available, inspect at most one original listing if time permits, save an actual search capture and a concise digest under .workflow/digests/. Run tracker views. Report sources that could not be checked; do not claim the full daily sweep or an autonomous full application passed. Stop after this bounded test.\n'
        def record(kind,**fields):
            with log.open('a') as f: f.write(json.dumps({'time':w.now(),'event':kind,**fields})+'\n')
        record('started',test=test,scheduled=scheduled,budget_seconds=max_seconds)
        child=subprocess.Popen(cli_command(),stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,start_new_session=True,text=True,bufsize=1,
                               env={**os.environ,'JOB_SEARCH_RUN_ID':stamp})
        child.stdin.write(prompt);child.stdin.close()
        w.write_json(STATE/'active.json',{'pid':child.pid,'stage':'daily-test' if test else 'daily','started':w.now(),'log':str(log.relative_to(ROOT))})
        sel=selectors.DefaultSelector();sel.register(child.stdout,selectors.EVENT_READ,'stdout');sel.register(child.stderr,selectors.EVENT_READ,'stderr')
        deadline=dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=max_seconds)
        reason=None
        try:
            while sel.get_map():
                control=w.read_json(STATE/'control.json',{})
                if control.get('paused'):
                    reason='Paused';stop_child(child);break
                if dt.datetime.now(dt.timezone.utc)>=deadline:
                    reason='Wall-clock budget exceeded';stop_child(child);break
                for stream,_ in sel.select(timeout=1):
                    line=stream.fileobj.readline()
                    if not line: sel.unregister(stream.fileobj);continue
                    if stream.data=='stderr': continue # Never persist raw stderr.
                    try: e=json.loads(line)
                    except ValueError: continue
                    item=e.get('item',{})
                    if item.get('type')=='mcp_tool_call':
                        # Tool name/status only, never arguments or response content.
                        record('browser_tool',tool=item.get('tool'),status=item.get('status'))
                    elif e.get('type') in ('turn.completed','turn.failed'):
                        record(e['type'])
            code=child.wait(timeout=5)
        finally:
            stop_child(child);sel.close();child.stdout.close();child.stderr.close();w.write_json(STATE/'active.json',{})
        digest=STATE/'digests'/f'{stamp}.md'
        if not digest.exists():
            reason=reason or 'CLI returned without a saved stage digest; coverage unverified'
            digest.parent.mkdir(exist_ok=True)
            digest.write_text(f'# Run incomplete — {stamp}\n\n{reason}. Exit code: {code}.\n\nSee {log.relative_to(ROOT)}. Do not infer completed stages or submitted applications. Inspect any Submission unconfirmed rows read-only before proceeding.\n')
        tab_log=STATE/'runs'/f'{stamp}-tabs.jsonl'
        helper_tab_log=STATE/'runs'/f'{stamp}-tabs-helper.jsonl'
        tab_events=[]
        authoritative_tab_log=tab_log if tab_log.exists() else helper_tab_log
        if authoritative_tab_log.exists():
            for line in authoritative_tab_log.read_text(encoding='utf-8').splitlines():
                try:tab_events.append(json.loads(line))
                except ValueError:continue
        summaries=[event for event in tab_events if event.get('event')=='tab_lifecycle_summary']
        if summaries:
            lifecycle={key:sum(int(item.get(key,0) or 0) for item in summaries)
                       for key in ('created','reused','closed','remaining_automation_tabs','job_tabs_created',
                                   'job_tabs_closed','welcome_tabs_created','welcome_tabs_closed',
                                   'remaining_job_tabs','remaining_welcome_tabs')}
            lifecycle['uncertain_ids']=[value for item in summaries for value in item.get('remaining_tab_ids',[])]
            lifecycle['source']='tab lifecycle sidecar'
        else:
            lifecycle={'created':None,'reused':None,'closed':None,'remaining_automation_tabs':None,
                       'job_tabs_created':None,'job_tabs_closed':None,'welcome_tabs_created':None,
                       'welcome_tabs_closed':None,'remaining_job_tabs':None,'remaining_welcome_tabs':None,
                       'uncertain_ids':['No machine tab lifecycle report was emitted'],'source':'unverified'}
        record('tab_lifecycle_summary',**lifecycle,sidecar=str(authoritative_tab_log.relative_to(ROOT)))
        if not test and not stage:
            import terminal_cleanup
            terminal_cleanup.after_normal_run(w)
        status='Blocked' if reason or code else 'CLI completed; inspect saved digest for stage coverage'
        result={'status':status,'exit_code':code,'reason':reason,'test':test,'log':str(log.relative_to(ROOT)),'digest':str(digest.relative_to(ROOT)),'finished_at':w.now()}
        record('finished',**result);w.write_json(STATE/'last-daily-run.json',result)
        w.event('daily_finished',**result)
        return result

def unit_files():
    python=shutil.which('python3') or '/usr/bin/python3'
    service=lambda test: f'''[Unit]
Description=Naukri workflow {'read-only test' if test else 'daily run in configured mode'}
[Service]
Type=oneshot
WorkingDirectory={ROOT}
ExecStart={python} {ROOT}/workflow daily {'--test --max-seconds 240' if test else '--scheduled --sources naukri,linkedin --fresh-only --review-limit 10 --page-cap 1 --query-cap 2 --max-seconds 1800'}
TimeoutStartSec={'270' if test else '1830'}
KillMode=control-group
StandardOutput=null
StandardError=null
Environment=PATH={os.environ.get('PATH','/usr/bin:/bin')}
'''
    return {UNIT+'.service':service(False),UNIT+'-test.service':service(True),UNIT+'.timer':f'''[Unit]
Description=Job search at 08:30, 12:30, 16:30, 20:30 Asia/Kolkata
[Timer]
OnCalendar=*-*-* 08,12,16,20:30:00 Asia/Kolkata
AccuracySec=1s
Persistent=false
Unit={UNIT}.service
[Install]
WantedBy=timers.target
'''}

def install(w):
    gate(w)
    units=Path.home()/'.config/systemd/user';units.mkdir(parents=True,exist_ok=True)
    preview=STATE/'schedule';preview.mkdir(exist_ok=True)
    for name,body in unit_files().items():
        (preview/name).write_text(body);(units/name).write_text(body)
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    import control,job_config
    control.sync(job_config.load(),paused=False)
    w.write_json(STATE/'daily-authorization.json',{'authorized':True,'source':'User standing autonomous daily application authorization; private probe passed','blocked_requisitions':[['unitedhealthgroup','2388999']]})
    subprocess.run(['systemctl','--user','enable','--now',UNIT+'.timer'],check=True,capture_output=True)
    w.event('daily_schedule_enabled',timezone='Asia/Kolkata',times=['08:30','12:30','16:30','20:30'],persistent=False)
    return schedule_status()

def schedule_status():
    p=subprocess.run(['systemctl','--user','show',UNIT+'.timer','--property=ActiveState','--property=NextElapseUSecRealtime'],text=True,capture_output=True)
    return {'timer':UNIT+'.timer','systemd':p.stdout.strip(),'timezone':'Asia/Kolkata','next_four_slots':[s.isoformat() for s in next_slots()]}

def pause_timer():
    subprocess.run(['systemctl','--user','stop',UNIT+'.timer'],capture_output=True)

def resume_timer(w):
    gate(w)
    if not (Path.home()/'.config/systemd/user'/f'{UNIT}.timer').is_file():
        raise ValueError('Daily schedule not installed; use start after the private probe passes.')
    w.write_json(STATE/'control.json',{'paused':False,'submission_enabled':True,'schedule_enabled':True})
    subprocess.run(['systemctl','--user','start',UNIT+'.timer'],check=True,capture_output=True)
    return schedule_status()
