"""Pause/resume control shared by the CLI, the pipeline and the UI (single implementation)."""
import json
import subprocess

from local_paths import ROOT

STATE=ROOT/'.workflow'
UNIT='job-search-daily'

def read():
    try:return json.loads((STATE/'control.json').read_text())
    except (OSError,ValueError):return {}

def is_paused():
    return bool(read().get('paused'))

def sync(config,paused=None):
    """Persist explanatory flags; config is authoritative and pause is the only runtime veto."""
    import job_config
    paused=is_paused() if paused is None else paused
    value={'paused':bool(paused),'submission_enabled':not paused and any(job_config.live_provider_enabled(config,p) for p in ('naukri','linkedin')),
           'schedule_enabled':not paused and config['schedule']['enabled'],
           'scheduled_application_enabled':not paused and any(job_config.live_provider_enabled(config,p,timer=True) for p in ('naukri','linkedin')),
           'provider_modes':{p:{'live':not paused and job_config.live_provider_enabled(config,p),'scheduled':not paused and job_config.live_provider_enabled(config,p,timer=True),
              'max_submissions_per_run':config['portal_workers'][p].get('max_submissions_per_run',1)} for p in ('naukri','linkedin')}}
    _write(value);return value

def _write(value):
    STATE.mkdir(exist_ok=True)
    tmp=STATE/'control.json.tmp';tmp.write_text(json.dumps(value,indent=2)+'\n');tmp.replace(STATE/'control.json')

def pause():
    """Stop new work at the next gate and stop the timer. An in-flight claimed attempt still finishes its own stop."""
    import job_config
    sync(job_config.load(),paused=True)
    subprocess.run(['systemctl','--user','stop',UNIT+'.timer'],capture_output=True)
    return {'paused':True}

def resume(config):
    import job_config
    scheduled=bool(config['schedule']['enabled'])
    sync(config,paused=False)
    if scheduled:subprocess.run(['systemctl','--user','start',UNIT+'.timer'],check=True)
    return {'paused':False,'timer':scheduled}
