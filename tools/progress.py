"""Run progress events (one jsonl per run) readable by the CLI, systemd runs and the UI."""
import datetime as dt
import json
import os

from local_paths import ROOT

STATE=ROOT/'.workflow'
_current={'run_id':None,'seq':0}

def _dir():
    folder=STATE/'progress';folder.mkdir(parents=True,exist_ok=True);return folder

def _now():return dt.datetime.now(dt.timezone.utc).isoformat()

def start(run_id,*,trigger,dry_run):
    _current.update(run_id=run_id,seq=0)
    (_dir()/'current.json').write_text(json.dumps({'run_id':run_id,'started_at':_now(),'trigger':trigger,
        'dry_run':bool(dry_run),'status':'running','pid':os.getpid()})+'\n')
    emit('Run started ('+trigger+(', dry run' if dry_run else '')+')',kind='start')

def emit(message,*,kind='info',**fields):
    run_id=_current['run_id']
    if not run_id:return
    _current['seq']+=1
    with (_dir()/f'{run_id}.jsonl').open('a') as handle:
        handle.write(json.dumps({'seq':_current['seq'],'at':_now(),'kind':kind,'message':str(message)[:400],**fields})+'\n')

def finish(status,summary=None):
    run_id=_current['run_id']
    if not run_id:return
    emit('Run finished: '+str(status),kind='finish',status=status,summary=summary)
    path=_dir()/'current.json'
    try:data=json.loads(path.read_text())
    except (OSError,ValueError):data={'run_id':run_id}
    data.update(status=status,finished_at=_now());path.write_text(json.dumps(data)+'\n')
    _current.update(run_id=None,seq=0)
    files=sorted(_dir().glob('*.jsonl'),key=lambda p:p.stat().st_mtime)
    for old in files[:-40]:old.unlink(missing_ok=True)

def read(run_id=None,since=0):
    folder=_dir()
    try:current=json.loads((folder/'current.json').read_text())
    except (OSError,ValueError):current={}
    run_id=run_id or current.get('run_id')
    events=[]
    if run_id and (folder/f'{run_id}.jsonl').is_file():
        for line in (folder/f'{run_id}.jsonl').read_text().splitlines():
            try:item=json.loads(line)
            except ValueError:continue
            if item['seq']>since:events.append(item)
    return {'run':current,'events':events,'next':events[-1]['seq'] if events else since}
