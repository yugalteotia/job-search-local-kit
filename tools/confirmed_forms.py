"""Native application-field inspection and filling from confirmed candidate facts only.

No answers/contact values are logged. Progress stores question hashes and fact references.
"""
import hashlib
import json
import datetime as dt
import calendar
import re
import candidate
from local_paths import ROOT

FIELDS=r'''root=>{
 const r=[...document.querySelectorAll(root)].find(e=>e.getClientRects().length);if(!r)return [];
 const visible=e=>e.getClientRects().length&&!e.disabled;const fields=[],seen=new Set();
 for(const e of r.querySelectorAll('input:not([type=hidden]):not([type=file]):not([type=password]),textarea,select,[role=combobox]')){
  if(!visible(e)||['submit','button'].includes(e.type))continue;
  const type=e.type|| (e.tagName==='SELECT'?'select-one':'text');
  const group=(type==='radio'||type==='checkbox')?e.closest('fieldset,[role=radiogroup],.fb-dash-form-element'):null;
  const key=group||e;if(seen.has(key))continue;seen.add(key);
  const label=id=>[...r.querySelectorAll('label')].find(l=>l.htmlFor===id)?.innerText?.trim()||'';
  let question=(group?.querySelector('legend,.fb-dash-form-element__label,h3')?.innerText||e.getAttribute('aria-label')||label(e.id)||e.placeholder||'').trim();
  if(!question){question=(e.closest('.fb-dash-form-element')?.querySelector('label')?.innerText||'').trim();}
  question=[...new Set(question.split(/\n+/).map(x=>x.trim()).filter(Boolean))].join(' ');
  const idx=fields.length;key.setAttribute('data-jobkit-field',String(idx));
  const opts=group?[...group.querySelectorAll('input')].filter(x=>x.type===type).map((x,i)=>{
    x.setAttribute('data-jobkit-option',String(i));return {label:label(x.id)||x.closest('label')?.innerText?.trim()||'',value:x.value,index:i,control_id:x.id};}):
    e.tagName==='SELECT'?[...e.options].map(o=>({label:o.text.trim(),value:o.value})):[];
  fields.push({index:idx,control_id:group?null:e.id,question,type,pattern:e.getAttribute('pattern'),inputmode:e.getAttribute('inputmode'),step:e.getAttribute('step'),required:!!e.required||e.getAttribute('aria-required')==='true'||/\*/.test(question)||/\bRequired\b/.test(question),options:opts});
 }
 return fields;}'''

def key(field):
    return hashlib.sha256((field['question']+'|'+field['type']+'|'+ '|'.join(x['label'] for x in field.get('options',[]))).encode()).hexdigest()

PHONE_LABELS={'phone','phone number','mobile','mobile number','mobile phone','mobile phone number','contact number'}

def contact_label(field):
    label=re.sub(r'[^a-z0-9]+',' ',field['question'].lower()).strip()
    return re.sub(r'\s+required$', '', label)

def phone_field(field):
    return field['type'] in ('text','tel','number') and contact_label(field) in PHONE_LABELS

def confirmed_phone(identity):
    # The private confirmed profile uses an Indian international number. Fail
    # closed on a different/malformed prefix rather than guessing a country.
    raw=str(identity.get('phone','')).strip()
    if not re.fullmatch(r'\+91[\s().-]*[0-9\s().-]+',raw):return None
    digits=re.sub(r'\D','',raw)[2:]
    return digits if re.fullmatch(r'[6-9][0-9]{9}',digits) else None

def phone_readback_matches(retained,value):
    # Accept normal display separators only; never another prefix or number.
    return bool(re.fullmatch(r'[0-9\s().-]+',retained)) and re.sub(r'\D','',retained)==str(value)

def answer(field,profile,job_req=None):
    if job_req:
        path=ROOT/'.workflow/private-candidate-confirmations'/('linkedin-'+str(job_req)+'.json')
        if path.exists():
            saved=json.loads(path.read_text())
            entry=next((x for x in saved.get('fields',[]) if x['key']==key(field)),None)
            invalid_experience=saved.get('experience_answers_valid') is False and re.search(r'experience',field['question'],re.I)
            if str(saved.get('requisition_id'))==str(job_req) and saved.get('confirmed_by_user') is True and entry and not invalid_experience:
                return entry['answer'],'user_confirmed_saved_draft:'+entry['key']
    q=' '.join(dict.fromkeys(x.strip() for x in field['question'].splitlines() if x.strip()))
    normalized=re.sub(r'[^a-z0-9]+',' ',q.lower()).strip();c=candidate.load();identity=c['identity']
    local_phone=confirmed_phone(identity)
    if phone_field(field):
        return (local_phone,'confirmed_identity:phone') if local_phone else (None,None)
    if contact_label(field)=='phone country code':
        return ('India (+91)','confirmed_identity:phone country code') if local_phone else (None,None)
    contact={
        'email':identity['email'],'email address':identity['email'],
        'first name':identity['name'].split()[0],'last name':identity['name'].split()[-1],
        'full name':identity['name'],'name':identity['name'],
        'mobile phone number':local_phone,'phone number':local_phone,
        'mobile number':local_phone,
        'city':identity['location'].split(',')[0].strip(),'current city':identity['location'].split(',')[0].strip(),
        'location city':identity['location'].split(',')[0].strip(),
        'country':identity['location'].split(',')[-1].strip(),'phone country code':'India (+91)',
    }
    if normalized in contact:return contact[normalized],'confirmed_identity:'+normalized
    if re.search(r'country.*(?:currently reside|currently live|residence)|current country|country of residence',normalized):
        return identity['location'].split(',')[-1].strip(),'confirmed_identity:country'
    if re.search(r'salary expectations|expected salary',normalized):
        line=next((x for x in profile.splitlines() if x.startswith('- Expected CTC:')),'')
        m=re.search(r'₹\s*([0-9.]+)\s*LPA\s*(?:as\s*)?(?:fixed|of fixed)',line,re.I)
        if m:return 'INR '+m.group(1)+' LPA fixed pay','confirmed_profile:expected_fixed_pay'
        return None,None
    if re.search(r'(?:link.*linkedin|linkedin.*(?:profile|url|link))',normalized):
        return identity['linkedin'],'confirmed_identity:linkedin'
    if re.search(r'(?:reasons? for.*job change|why.*job change)',normalized):
        block=profile[profile.find('- Reason for leaving IDeaS'):].split('\n- Career gap')[0]
        quotes=re.findall(r'[“\"]([^”\"]+)[”\"]',block)
        if re.search(r'(?:from|after|since).*?(?:1 October 2026|30 September 2026)',block,re.I) and quotes:
            return quotes[-1],'confirmed_profile:post_employment_reason'
        return None,None
    if re.search(r'what state.*currently|which state.*reside|confirm.*state.*live',normalized):
        city=identity['location'].split(',')[0].strip().casefold()
        states={'pune':'Maharashtra','mumbai':'Maharashtra','bengaluru':'Karnataka','bangalore':'Karnataka','hyderabad':'Telangana','chennai':'Tamil Nadu','noida':'Uttar Pradesh','gurugram':'Haryana','delhi':'Delhi'}
        if city in states:return states[city],'confirmed_identity:state_from_city'
    if re.search(r'total.*(?:years|experience).*industry|total.*professional experience',normalized):
        days=0;records=json.loads((ROOT/'employment_dates.json').read_text())['employment']
        for job in records:
            if 'start' in job and 'end' in job:
                start=dt.date.fromisoformat(job['start']);end=min(dt.date.today(),dt.date.fromisoformat(job['end']))
            elif 'start_month' in job and 'end_month' in job:
                y,m=map(int,job['start_month'].split('-'));start=dt.date(y,m,calendar.monthrange(y,m)[1])
                y,m=map(int,job['end_month'].split('-'));end=min(dt.date.today(),dt.date(y,m,1))
            else:return None,None
            days+=max(0,(end-start).days)
        return str(int(days/365.25)),'confirmed_dates:completed_total_years_lower_bound'
    if re.search(r'major.*tech stacks|technolog.*worked',normalized):
        src=candidate.sources();production=src['work']+' '+src['production_profile']
        names=[('Java',r'\bJava\b'),('Spring Boot',r'Spring Boot'),('REST APIs',r'REST'),('gRPC',r'gRPC'),('MongoDB',r'MongoDB'),('Ember.js',r'Ember'),('TypeScript',r'TypeScript'),('GitHub Actions',r'GitHub Actions'),('AWS ECS/Fargate (limited deployment exposure)',r'ECS.*Fargate')]
        return '; '.join(label for label,rx in names if re.search(rx,production,re.I)),'confirmed_profile:production_technologies'
    if 'ctc' in normalized and re.search(r'rsu|esop|components',normalized):
        equity=re.search(r'^-\s*(?:Last\s+)?(?:RSU/ESOP|RSU|ESOP)(?:\s+compensation)?\s*:\s*(.+)$',profile,re.M|re.I)
        if not equity or re.search(r'unknown|not confirmed|not recorded',equity.group(1),re.I):return None,None
        fixed=candidate.answer('What is your current fixed CTC?',profile)[0]
        variable=candidate.answer('What is your current variable CTC?',profile)[0]
        if fixed and variable:return 'Not currently employed. Last annual compensation: '+fixed+'; '+variable+'; RSU/ESOP: '+equity.group(1),'confirmed_profile:last_compensation_components'
        return None,None
    result,fact=candidate.answer(q,profile)
    if result is not None:return result,fact
    if field['type']=='checkbox' and re.search(r'which.*skills|select.*skills|technical skills',normalized):
        production=bool(re.search(r'production|professional|work experience',normalized));values=[]
        for option in field.get('options',[]):
            if any(s['basis'] in (('work','profile_work') if production else ('work','profile_work','project','training'))
                   and any(re.search(p,option['label'],re.I) for p in s['patterns']) for s in c['skills'].values()):
                values.append(option['label'])
        if values:return values,'confirmed_skill_evidence'
    return None,None

def fill(scope,field,value,*,verify_only=False):
    """Return True only after the exact control retains its confirmed answer."""
    selector='[id='+json.dumps(field['control_id'])+']' if field.get('control_id') else '[data-jobkit-field="'+str(field['index'])+'"]'
    loc=scope.locator(selector);typ=field['type']
    if typ in ('radio','checkbox'):
        values=value if isinstance(value,list) else [str(value)]
        choices=[o for o in field['options'] if o['label'].strip().casefold() in {str(v).strip().casefold() for v in values}]
        if len(choices)!=len(values):return False
        indices={x['index'] for x in choices}
        def control_for(o):
            return scope.locator('[id='+json.dumps(o['control_id'])+']') if o.get('control_id') else loc.locator('[data-jobkit-option="'+str(o['index'])+'"]')
        def toggle(o):
            ctl=control_for(o)
            labels=scope.locator('label[for='+json.dumps(o['control_id'])+']') if o.get('control_id') else None
            if labels is not None and labels.count()==1:labels.click(timeout=5000)
            else:ctl.set_checked(o['index'] in indices)
        if typ=='radio' and not verify_only:
            if len(choices)!=1:return False
            if not control_for(choices[0]).is_checked():toggle(choices[0])
        for o in field['options']:
            control=control_for(o)
            desired=o['index'] in indices
            if verify_only and control.is_checked()!=desired:return False
            if not verify_only and typ=='checkbox' and control.is_checked()!=desired:toggle(o)
            if control.is_checked()!=desired:return False
        return True
    if typ.startswith('select'):
        matches=[o for o in field['options'] if o['label'].strip().casefold()==str(value).strip().casefold()]
        if not matches and str(value)=='India (+91)':
            matches=[o for o in field['options'] if re.search(r'\bIndia\b',o['label'],re.I) and re.search(r'\+91(?!\d)',o['label'])]
        if contact_label(field)=='phone country code' and len(matches)!=1:return False
        if not matches and re.search(r'country|state',field['question'],re.I):
            matches=[o for o in field['options'] if re.fullmatch(r'(?:Country |State )?Not listed',o['label'].strip(),re.I)]
            if len(matches)==1:field['selected_not_listed']=True
        if len(matches)!=1:return False
        if verify_only:return loc.input_value()==matches[0]['value']
        loc.select_option(value=matches[0]['value']);return loc.input_value()==matches[0]['value']
    if verify_only:
        retained=loc.input_value().strip()
        if phone_field(field):return phone_readback_matches(retained,value)
        if re.search(r'location.*city|current city|^city$',field['question'],re.I):
            field['saved_city_check']={'empty':not retained,'same_city':retained.split(',')[0].strip().casefold()==str(value).strip().casefold(),'india': 'india' in retained.casefold(),'known_region':'maharashtra' in retained.casefold()}
            return retained.casefold()==str(value).strip().casefold() or (retained.split(',')[0].strip().casefold()==str(value).strip().casefold() and ('india' in retained.casefold() or 'maharashtra' in retained.casefold()))
        return retained==str(value).strip()
    loc.fill(str(value))
    if phone_field(field):return phone_readback_matches(loc.input_value().strip(),value)
    if re.search(r'location.*city|current city|^city$',field['question'],re.I):
        # LinkedIn requires a selected city entity, not merely text in its autocomplete.
        field['autocomplete_controls']=loc.page.evaluate("v=>[...document.querySelectorAll('li,[role=option],button')].filter(e=>e.getClientRects().length&&e.innerText.trim().toLowerCase().startsWith(v.toLowerCase())).map(e=>({tag:e.tagName,role:e.getAttribute('role'),classes:e.className,india:/India|Maharashtra/i.test(e.innerText)}))",str(value))
        menu_id=loc.get_attribute('aria-controls') or loc.get_attribute('aria-owns')
        menu=loc.page.locator('[id='+json.dumps(menu_id)+']') if menu_id else scope
        # Exact city before the first comma excludes districts, neighbourhoods and similarly named areas.
        options=menu.get_by_role('option').filter(has_text=re.compile(r'^\s*'+re.escape(str(value))+r'\s*,[^\n]*\bIndia\s*$',re.I))
        try:options.first.wait_for(state='visible',timeout=5000)
        except Exception:pass
        field['autocomplete_controls']=loc.page.evaluate("v=>[...document.querySelectorAll('li,[role=option],button,div,span')].filter(e=>e.getClientRects().length&&e.innerText?.trim().length<120&&e.innerText.trim().toLowerCase().startsWith(v.toLowerCase())).map(e=>({tag:e.tagName,role:e.getAttribute('role'),classes:e.className,india:/India|Maharashtra/i.test(e.innerText)}))",str(value))
        field['autocomplete_input']=loc.evaluate("e=>({role:e.getAttribute('role'),autocomplete:e.getAttribute('aria-autocomplete'),expanded:e.getAttribute('aria-expanded'),controls:e.getAttribute('aria-controls'),owns:e.getAttribute('aria-owns')})")
        if options.count()==1:
            leaf=options.locator('span').filter(has_text=re.compile(r'^\s*'+re.escape(str(value))+r'\b[\s\S]*(?:India|Maharashtra)',re.I))
            if leaf.count()==1:leaf.click(timeout=5000,force=True)
            else:options.click(timeout=5000,force=True)
        loc.press('Tab')
    retained=loc.input_value().strip()
    return retained==str(value).strip() or (bool(re.search(r'location.*city|current city|^city$',field['question'],re.I)) and retained.lower().startswith(str(value).lower()+','))

def current_value(scope,field):
    """Read only an application field the user explicitly completed; never credentials."""
    if field['type']=='password':return None
    if field['type'] in ('radio','checkbox'):
        labels=[]
        for o in field.get('options',[]):
            loc=scope.locator('[id='+json.dumps(o['control_id'])+']') if o.get('control_id') else scope.locator('[data-jobkit-option='+json.dumps(str(o['index']))+']')
            if loc.is_checked():labels.append(o['label'])
        return labels if field['type']=='checkbox' else labels[0] if len(labels)==1 else None
    selector='[id='+json.dumps(field['control_id'])+']' if field.get('control_id') else '[data-jobkit-field='+json.dumps(str(field['index']))+']'
    loc=scope.locator(selector);value=loc.input_value().strip()
    if field['type'].startswith('select'):
        choice=next((o['label'] for o in field['options'] if o['value']==value),None)
        return None if not choice or re.match(r'select (?:an? )?option',choice,re.I) else choice
    return value or None

def validate_answer(field,value):
    """Reject experience beyond the upper bound of confirmed employment dates."""
    q=field['question'].casefold()
    if not re.search(r'experience',q) or not re.search(r'years?|months?',q):return None
    text=str(value);units=re.findall(r'(\d+(?:\.\d+)?)\s*(years?|yrs?|months?|mos?)',text,re.I)
    if units:years=sum(float(n)/(12 if u.casefold().startswith(('month','mo')) else 1) for n,u in units)
    else:
        m=re.match(r'^\s*(\d+(?:\.\d+)?)\b',text)
        if not m:return 'experience_answer_needs_numeric_date_basis'
        years=float(m[1])/(12 if 'month' in q and 'year' not in q else 1)
    total=bool(re.search(r'total|industry|professional',q));intervals=[]
    for j in json.loads((ROOT/'employment_dates.json').read_text())['employment']:
        if not total and not j.get('java'):continue
        if 'start' in j and 'end' in j:a=dt.date.fromisoformat(j['start']);b=dt.date.fromisoformat(j['end'])
        elif 'start_month' in j and 'end_month' in j:
            y,m=map(int,j['start_month'].split('-'));a=dt.date(y,m,1)
            y,m=map(int,j['end_month'].split('-'));b=dt.date(y,m,calendar.monthrange(y,m)[1])
        else:return 'experience_dates_incomplete'
        intervals.append((a,min(b,dt.date.today())))
    merged=[]
    for a,b in sorted(intervals):
        if merged and a<=merged[-1][1]:merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
        else:merged.append((a,b))
    upper=sum(max(0,(b-a).days) for a,b in merged)/365.25
    return 'candidate_experience_date_conflict' if years<0 or years>upper+.005 else None

def format_answer(field,value):
    """Use completed years for integer experience controls; never a ceiling."""
    q=field['question'].casefold()
    integer=(field.get('type')=='number' and field.get('step') in (None,'','1')) or field.get('inputmode')=='numeric' or bool(re.fullmatch(r'\\d\+|\[0-9\]\+',field.get('pattern') or ''))
    if integer and 'experience' in q and 'year' in q and re.fullmatch(r'\d+(?:\.\d+)?',str(value)):
        from decimal import Decimal,ROUND_FLOOR
        return str(Decimal(str(value)).to_integral_value(rounding=ROUND_FLOOR))
    return value
