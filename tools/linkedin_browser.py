"""Dedicated owner-only LinkedIn automation profile. Never reuse personal Chrome."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

from local_paths import ROOT
from naukri_direct_playwright import _playwright

PRIVATE=Path.home()/'.local/share/job-search-private/linkedin-direct-playwright'
PROFILE=PRIVATE/'user-data'

@contextlib.contextmanager
def chrome(*,read_only=False):
    if PRIVATE.is_symlink() or PROFILE.is_symlink():raise RuntimeError('Private profile cannot be a symlink')
    if PRIVATE.resolve().is_relative_to(ROOT) or PRIVATE.resolve().is_relative_to((Path.home()/'.config/google-chrome').resolve()):
        raise RuntimeError('Refusing repository or personal Chrome authentication')
    old=os.umask(0o077)
    try:
        PRIVATE.mkdir(parents=True,exist_ok=True,mode=0o700);PROFILE.mkdir(exist_ok=True,mode=0o700)
        PRIVATE.chmod(0o700);PROFILE.chmod(0o700)
        with (PRIVATE/'profile.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with _playwright()() as pw:
                ctx=pw.chromium.launch_persistent_context(str(PROFILE),channel='chrome',headless=False,
                    accept_downloads=False,service_workers='block' if read_only else 'allow',timeout=30000)
                try:yield ctx
                finally:ctx.close()
    finally:os.umask(old)

def classify_health_sample(sample):
    path=sample.get('path','')
    verification=('/checkpoint' in path or '/challenge' in path or sample.get('captcha_visible') or sample.get('verification_heading'))
    if verification:return 'verification_required'
    if sample.get('restriction_heading'):return 'account_restricted'
    if (path.startswith(('/login','/uas/login','/authwall')) or sample.get('password_fields',0)>0 or sample.get('login_fields',0)>0):
        return 'authentication_required'
    if path.startswith('/feed') and (sample.get('feed_links',0)>0 or sample.get('authenticated_controls',0)>0):
        return 'authenticated'
    if sample.get('ready_state') in ('loading','interactive') or sample.get('loading_indicator'):
        return 'page_loading'
    return 'selector_or_render_failure'

def status(page):
    navigation_error=None
    try:page.goto('https://www.linkedin.com/feed/',wait_until='domcontentloaded',timeout=30000)
    except Exception as exc:navigation_error=type(exc).__name__
    try:page.wait_for_selector('.global-nav,a[href*="/feed/"],button[aria-label="Me"],button[aria-label^="Me,"],a[href*="/messaging/"]',timeout=10000)
    except Exception:pass
    samples=[];stable=0
    for unused in range(12):
        sample={'path':urlsplit(page.url).path,
                'feed_links':page.locator('a[href*="/feed/"]:visible').count(),
                'password_fields':page.locator('input[type=password]:visible').count()}
        sample.update(page.evaluate(r"""()=>{
          const visible=e=>!!e&&!!e.getClientRects().length;
          const heading=[...document.querySelectorAll('h1')].filter(visible).map(e=>e.innerText).join(' ');
          const authenticated=[...document.querySelectorAll('button[aria-label="Me"],button[aria-label^="Me,"],[data-view-name="nav-profile-menu"],a[href*="/messaging/"]')].filter(visible);
          return {ready_state:document.readyState,
            authenticated_control_kinds:authenticated.map(e=>e.matches('[data-view-name="nav-profile-menu"]')?'profile_menu':e.tagName==='BUTTON'?'profile_button':'messaging_link'),
            authenticated_controls:authenticated.length,
            login_fields:[...document.querySelectorAll('input[name="session_key"],input[autocomplete="username"]')].filter(visible).length,
            captcha_visible:[...document.querySelectorAll('iframe[src*="captcha"],form[action*="checkpoint"]')].some(visible),
            verification_heading:/security (?:check|verification)|verify (?:your|you are)|confirm your identity/i.test(heading),
            restriction_heading:/account (?:restricted|suspended)|temporarily restricted|application limit|too many requests/i.test(heading),
            loading_indicator:[...document.querySelectorAll('[aria-busy="true"],[role="progressbar"]')].some(visible)};}"""))
        sample['state']=classify_health_sample(sample);samples.append(sample)
        stable=stable+1 if sample['state']=='authenticated' else 0
        if sample['state'] in ('verification_required','authentication_required','account_restricted') or stable>=2:break
        page.wait_for_timeout(500)
    state='authenticated' if stable>=2 else samples[-1]['state']
    if state=='authenticated' and stable<2:state='selector_or_render_failure'
    errors={'authentication_required':'linkedin_authentication_required','verification_required':'linkedin_verification_required',
            'account_restricted':'linkedin_account_restricted','page_loading':'linkedin_page_loading',
            'selector_or_render_failure':'linkedin_selector_or_render_failure'}
    return {'signed_in':stable>=2,'authentication_required':state=='authentication_required',
            'verification_required':state=='verification_required','session_state':state,'error_kind':errors.get(state),
            'navigation_error':navigation_error,'health_samples':samples}

def login(timeout=600):
    with chrome() as ctx:
        page=ctx.pages[0] if ctx.pages else ctx.new_page()
        state=status(page)
        if state['signed_in']:return state
        page.goto('https://www.linkedin.com/login',wait_until='domcontentloaded')
        print('Dedicated LinkedIn login window is open. Sign in directly there; no credentials are read or printed.',flush=True)
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if urlsplit(page.url).path.startswith('/feed') and page.locator('a[href*="/feed/"]').count():
                return {'signed_in':True,'verification_required':False}
            page.wait_for_timeout(1000)
        return {'signed_in':False,'handoff':'Run python3 tools/linkedin_browser.py login and sign in directly in that window.'}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['login','check']);p.add_argument('--timeout',type=int,default=600)
    a=p.parse_args()
    if a.action=='login':print(json.dumps(login(a.timeout)))
    else:
        from naukri_discovery import readonly_route
        with chrome(read_only=True) as c:
            c.route('**/*',readonly_route);print(json.dumps(status(c.pages[0] if c.pages else c.new_page())))
