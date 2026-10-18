"""Bounded, read-only separate Codex CLI probe; never persist raw tool output."""
import datetime as dt
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def identity_proof(value):
    """Accept actual boolean fields in a tool result, never a substring match."""
    if isinstance(value, dict):
        if value.get('signed_in') is True and value.get('identity_match') is True:
            return True
        return any(identity_proof(v) for v in value.values())
    if isinstance(value,list):
        return any(identity_proof(v) for v in value)
    if isinstance(value,str):
        # MCP text can surround JSON with Markdown headings.
        for index, char in enumerate(value):
            if char != '{': continue
            try: parsed, _ = json.JSONDecoder().raw_decode(value[index:])
            except ValueError: continue
            if identity_proof(parsed): return True
    return False

def identity_observation(value):
    if isinstance(value,dict):
        if isinstance(value.get('signed_in'),bool) and isinstance(value.get('identity_match'),bool):
            return {k:value[k] for k in ('signed_in','identity_match')}
        for child in value.values():
            found=identity_observation(child)
            if found is not None: return found
    elif isinstance(value,list):
        for child in value:
            found=identity_observation(child)
            if found is not None: return found
    elif isinstance(value,str):
        for i,char in enumerate(value):
            if char!='{': continue
            try: parsed,_=json.JSONDecoder().raw_decode(value[i:])
            except ValueError: continue
            found=identity_observation(parsed)
            if found is not None: return found
    return None

def main():
    private = '--private' in sys.argv
    if private:
        from private_playwright import SECRET
        if not SECRET.is_file():
            print('Private token setup is required before the no-click retest. No browser attempt made.')
            return 2
    prompt = '''Perform ONE read-only Playwright connection attempt using browser_evaluate.
Return only whether the CURRENT page visibly establishes a signed-in account for Yugal.
Evaluate DOM text locally and return booleans and hostname only; do not return page text,
addresses, email contents, credentials, cookies, tokens, OTPs, or URLs with query strings.
Do not navigate, fill, click, upload, submit, read files, or retry a failed connection.
Use this evaluation: () => ({host:location.hostname, signed_in:/you are signed in/i.test(document.body.innerText), identity_match:/Yugal\\s+Teotia/i.test(document.body.innerText)})
If a connection/approval fails, report its error without connection URLs or secrets.
Final answer must be JSON: {"signed_in": boolean, "identity_match": boolean}.
Only set these true from the actual successful browser_evaluate result.'''
    cmd = ['codex','exec','--ephemeral','--skip-git-repo-check','--sandbox','read-only',
           '-c','approval_policy="never"','-c','mcp_servers.playwright.args=["-y","@playwright/mcp@latest","--extension","--snapshot-mode","none"]',
           '--json','-C',str(ROOT),'-']
    if private:
        # Override the server only for this child, leaving interactive config alone.
        cmd[cmd.index('mcp_servers.playwright.args=["-y","@playwright/mcp@latest","--extension","--snapshot-mode","none"]')] = 'mcp_servers.playwright.args=' + json.dumps([str(ROOT/'tools/private_playwright.py')])
        cmd[2:2] = ['-c','mcp_servers.playwright.command="python3"']
        prompt = prompt.replace('using browser_evaluate.', 'using browser_navigate once to https://uhg.taleo.net/careersection/10780/mysubmissions.ftl?lang=en then browser_evaluate.')
        prompt = prompt.replace('Do not navigate, fill,', 'Do not navigate elsewhere, fill,')
        prompt += '\nDo not read private credentials or extension UI. No approval clicks are allowed. This is a read-only submissions-list identity check, never an application action.'
        if '--gmail' in sys.argv:
            prompt = '''Perform exactly one read-only browser_navigate to https://mail.google.com/mail/u/0/#inbox, then browser_evaluate.
No clicks, approval, forms, file reads/writes, cookies, tokens or messages. Do not inspect email contents or inbox text.
Use only the Google account control's aria-label to establish the signed-in account.
Evaluate: () => { const a=document.querySelector('[aria-label*="Google Account"], [aria-label*="Google account"]'); const label=a?.getAttribute('aria-label')||''; return {host:location.hostname, signed_in:location.hostname==='mail.google.com' && !!a, identity_match:/teotia\\.yugal@gmail\\.com/i.test(label)}; }
Return no account label, email contents, full page text or URL queries. Do not retry a failed connection.
Final answer only JSON: {"signed_in":boolean,"identity_match":boolean}, based on the actual browser_evaluate result.
Do not claim success if the account control cannot establish the identity.'''
    start = dt.datetime.now(dt.timezone.utc).isoformat()
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    timed_out = False
    try:
        out, err = p.communicate(prompt, timeout=180)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(p.pid, signal.SIGTERM)
        try: out, err = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            out, err = p.communicate()
    # Allowlist log fields. Raw output can contain extension URLs; never save it.
    events=[]; actual=False; final={}; observation=None
    for line in out.splitlines():
        try: e=json.loads(line)
        except (ValueError, TypeError): continue
        item=e.get('item',{})
        if item.get('type')=='mcp_tool_call':
            tool=item.get('tool','')
            if tool in ('browser_evaluate','browser_tabs','browser_navigate'):
                events.append({'tool':tool,'status':item.get('status')})
                if tool=='browser_evaluate' and item.get('status')=='completed' and not item.get('error'):
                    actual = identity_proof(item.get('result',{}))
                    observation=identity_observation(item.get('result',{}))
                    events[-1]['result_keys']=list(item.get('result',{})) if isinstance(item.get('result'),dict) else []
        if item.get('type')=='agent_message':
            try: final=json.loads(item.get('text','').strip().removeprefix('```json').removesuffix('```').strip())
            except ValueError: pass
    combined=out+'\n'+err
    known=[s for s in ['Browser connection failed','Extension connection timed out','timed out awaiting tools/call after 300s','Not connected','Failed to connect','Operation not permitted','Authentication required'] if s in combined]
    passed=bool(not timed_out and p.returncode==0 and actual and final.get('signed_in') is True and final.get('identity_match') is True)
    result={'started_at':start,'finished_at':dt.datetime.now(dt.timezone.utc).isoformat(),
            'separate_cli':True,'read_only':True,'timeout_seconds':180,'timed_out':timed_out,
            'exit_code':p.returncode,'browser_events':events,'passed':passed,
            'errors':known,'failure':None if passed else ('Separate CLI exceeded 180-second bound; process group terminated.' if timed_out else 'Separate CLI did not establish signed-in identity; scheduling remains disabled.')}
    result['private_token_transport']=private
    result['observed_identity']=observation
    path=ROOT/'.workflow'/('browser-probe-private.json' if private else 'browser-probe.json');path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
    return 0 if passed else 1

if __name__=='__main__': raise SystemExit(main())
