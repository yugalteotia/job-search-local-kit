'use strict';
const token=(location.hash.match(/token=([^&]+)/)||[])[1]||sessionStorage.getItem('t')||'';
if(token){sessionStorage.setItem('t',token);history.replaceState(null,'',location.pathname);}
const $=id=>document.getElementById(id);
function el(tag,text,attrs){const e=document.createElement(tag);if(text!==undefined&&text!==null)e.textContent=String(text);
  for(const k in (attrs||{}))e.setAttribute(k,attrs[k]);return e;}
function safeLink(url,label){if(typeof url==='string'&&(url.startsWith('https://')||url.startsWith('/api/evidence/'))){
  const a=el('a',label);a.href=url;a.target='_blank';a.rel='noopener noreferrer';return a;}return el('span','');}
async function api(path,body){
  const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'X-UI-Token':token,'Content-Type':'application/json'},
    body:body===undefined?undefined:JSON.stringify(body)});
  const data=await r.json().catch(()=>({error:'Bad response'}));
  if(!r.ok)throw new Error(data.error||('HTTP '+r.status));return data;}
function table(cols,rows,cellFn){const t=el('table');const h=el('tr');cols.forEach(c=>h.appendChild(el('th',c)));t.appendChild(h);
  if(!rows.length){const r=el('tr');const c=el('td','None',{colspan:cols.length});c.className='muted';r.appendChild(c);t.appendChild(r);}
  rows.forEach(row=>{const r=el('tr');cellFn(row).forEach(c=>{const td=el('td');if(c instanceof Node)td.appendChild(c);else td.textContent=c==null?'':String(c);r.appendChild(td);});t.appendChild(r);});return t;}
function replace(id,...nodes){const n=$(id);n.replaceChildren(...nodes);}
function evidenceCell(slug){const s=el('span');if(!slug)return s;const b=el('button','files');b.onclick=async()=>{
  const list=await api('/api/evidence/'+slug);s.replaceChildren(...list.files.map(f=>{const a=safeLink('/api/evidence/'+slug+'/'+f,f);
    a.addEventListener('click',async ev=>{ev.preventDefault();const r=await fetch(a.href,{headers:{'X-UI-Token':token}});const blob=await r.blob();window.open(URL.createObjectURL(blob));});
    const d=el('div');d.appendChild(a);return d;}));};s.appendChild(b);return s;}
const tabs=['run','handoffs','tracker','portals'];
tabs.forEach(t=>{const b=el('button',t[0].toUpperCase()+t.slice(1));b.id='tab-'+t;b.onclick=()=>show(t);$('nav').appendChild(b);});
function show(t){tabs.forEach(x=>{$(x).classList.toggle('on',x===t);$('tab-'+x).classList.toggle('on',x===t);});
  if(t==='handoffs')loadHandoffs();if(t==='tracker')loadTracker('quick');if(t==='portals')loadPortals();}
let since=0,running=false,liveMode=false;
async function pollProgress(){try{const p=await api('/api/progress?since='+since);
  if(p.events.length){const log=$('log');p.events.forEach(e=>{log.appendChild(document.createTextNode(e.at.slice(11,19)+'  '+e.message+'\n'));});
    log.scrollTop=log.scrollHeight;since=p.next;}}catch(e){}}
async function refresh(){try{const s=await api('/api/status');running=s.run.running;
  liveMode=s.lane.manual_live_enabled;
  $('banner').textContent=(s.control.paused?'PAUSED · ':'')+(running?'run in progress':'idle')+' · timer '+(s.timer.active?'on':'off');
  const st=el('div');st.appendChild(el('b','Run Now: '));
  st.appendChild(document.createTextNode(Object.entries(s.lane.providers||{}).map(([p,v])=>p+': '+(v.manual_live_enabled?'live':'discovery only')+', scheduled '+(v.timer_live_enabled?'live':'off')+', max '+(v.max_new_attempts_per_run||v.max_submissions_per_run)+' new attempts/run').join(' · ')));
  st.appendChild(document.createTextNode(' · Dry run never uploads or submits'));
  $('btnrun').textContent=s.lane.manual_live_enabled?'Run live now':'Run discovery now';
  st.appendChild(document.createTextNode(' · other portals disabled · applied '+s.counts.applied+' · needs answer '+s.counts.needs_answer+' · uncertain '+s.counts.uncertain+' · urgent '+s.counts.urgent));
  replace('runstate',st);
  $('btnrun').disabled=$('btndry').disabled=running||s.control.paused;$('btnresume').disabled=!s.control.paused;$('btnpause').disabled=s.control.paused;
  const lr=s.last_run;const src=Object.entries(lr.sources||{});
  replace('lastrun',el('p',(lr.finished_at||'no run yet')+' · '+(lr.status||'')),table(['Source','Result','Leads','Error'],src,([n,v])=>[n,v.label,v.leads,v.error_kind||'']));
  if(running)pollProgress();}catch(e){$('banner').textContent=e.message;}}
async function post(path,body){try{const r=await api(path,body);if(r.started===false)alert(r.reason);}catch(e){alert(e.message);}refresh();}
$('btnrun').onclick=()=>{if(confirm(liveMode?'Start the configured native application workers? Independent provider limits are shown above; blocked new attempts consume a slot.':'Start discovery now? Unproven or disabled providers cannot submit.')){$('log').textContent='';since=0;post('/api/run',{dry_run:false});}};
$('btndry').onclick=()=>{$('log').textContent='';since=0;post('/api/run',{dry_run:true});};
$('btnpause').onclick=()=>post('/api/pause',{});$('btnresume').onclick=()=>post('/api/resume',{});
async function loadHandoffs(){const h=await api('/api/handoffs');
  replace('handoffs',el('h3','Needs answer / handoff'),
    table(['Company','Role','Needed','Link','Evidence'],h.needs_answer,r=>[r.employer,r.title,r.question||r.reason,safeLink(r.canonical_url,'open'),evidenceCell(r.slug)]),
    el('h3','Uncertain (locked, never retried)'),
    table(['Company','Role','Open question','Link','Evidence'],h.uncertain,r=>[r.employer,r.title,r.pending_question||'',safeLink(r.canonical_url,'check'),evidenceCell(r.slug)]),
    el('h3','Urgent responses'),table(['Company','Role','State','Link'],h.urgent,r=>[r.employer,r.title,r.state,safeLink(r.canonical_url,'open')]),
    el('h3','Applied'),table(['Company','Role','Date','Outcome','Evidence'],h.applied,r=>[r.employer,r.title,String(r.status_date||'').slice(0,10),r.outcome_label||'Applied',evidenceCell(r.slug)]));}
async function loadTracker(view){const t=await api('/api/tracker?view='+view);
  replace('trk',table(t.columns,t.rows,r=>t.columns.map(c=>c==='Direct link'?safeLink(r[c],'open'):r[c])));}
$('tq').onclick=()=>loadTracker('quick');$('td').onclick=()=>loadTracker('detailed');
async function loadPortals(){const p=await api('/api/portals');
  const caps=Object.entries(p.capabilities);
  const capTable=table(['Portal','Capability','Reason','Session','Application lane'],caps,([n,c])=>{
    const pill=el('span',c.capability);pill.className='pill '+c.capability;
    const on=n==='naukri'?p.naukri_worker_enabled:p.apply_enabled[n];
    const b=el('button',n==='naukri'?(on?'Disable lane':'Enable lane'):'Needs proof');b.className='act grey';
    if(n!=='naukri')b.disabled=true;
    else b.onclick=async()=>{try{await api('/api/portals/apply',{portal:n,enabled:!on});}catch(e){alert(e.message);}loadPortals();};
    return [n,pill,c.reason,c.session,b];});
  const srcTable=table(['Source','Kind','Discovery','Last success'],p.sources,s=>{
    const b=el('button',s.enabled?'Turn off':'Turn on');b.className='act grey';
    b.onclick=async()=>{try{await api('/api/portals/source',{section:s.section,name:s.name,enabled:!s.enabled});}catch(e){alert(e.message);}loadPortals();};
    return [s.name,s.section,b,s.last_successful_fetch||'never'];});
  replace('portals',el('h3','Capabilities'),capTable,el('p',p.note,{class:'muted'}),el('h3','Discovery sources'),srcTable);}
show('run');pollProgress();refresh();setInterval(refresh,4000);setInterval(()=>{if(running)pollProgress();},1500);
