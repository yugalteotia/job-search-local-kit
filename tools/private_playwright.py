"""Private extension credential setup and MCP launcher. Never prints credentials."""
import getpass
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

PRIVATE = Path.home() / '.config' / 'job-search-private'
SECRET = PRIVATE / 'playwright.json'

def load_private():
    for path, directory in ((PRIVATE, True), (SECRET, False)):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Private credential permissions must be directory 0700 and file 0600, owned by you.')
        if directory != stat.S_ISDIR(info.st_mode):
            raise ValueError('Invalid private credential path.')
    return json.loads(SECRET.read_text())

def setup():
    if not sys.stdin.isatty():
        raise ValueError('Run setup directly in your terminal; hidden input requires a TTY.')
    token = getpass.getpass('Paste the newly regenerated Playwright token (hidden): ').strip()
    token = token.removeprefix('PLAYWRIGHT_MCP_EXTENSION_TOKEN=').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        raise ValueError('Invalid token format; nothing saved.')
    profile = input('Chrome profile directory from chrome://version (Default or Profile N): ').strip()
    if not re.fullmatch(r'Default|Profile [0-9]+', profile):
        raise ValueError('Invalid profile directory; nothing saved.')
    if PRIVATE.is_symlink():
        raise ValueError('Refusing a symlinked private directory.')
    PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if PRIVATE.stat().st_uid != os.getuid():
        raise ValueError('Private directory must be owned by you.')
    PRIVATE.chmod(0o700)
    fd, temporary = tempfile.mkstemp(dir=PRIVATE)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump({'token':token,'profile':profile},f)
        os.replace(temporary,SECRET)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    print('Saved privately with mode 0600. Token was not printed. Clear your clipboard now.')

def launch():
    data=load_private()
    env=os.environ.copy()
    env['PLAYWRIGHT_MCP_EXTENSION_TOKEN']=data['token']
    env['PLAYWRIGHT_MCP_PROFILE_DIR_NAME']=data['profile']
    # Secret goes only to the proxy and upstream MCP, never argv or a prompt.
    proxy=Path(__file__).resolve().with_name('private_playwright_proxy.py')
    os.execvpe(sys.executable,[sys.executable,str(proxy)],env)

if __name__=='__main__':
    try:
        setup() if len(sys.argv)>1 and sys.argv[1]=='setup' else launch()
    except (ValueError,OSError,KeyError):
        print('Private Playwright setup is missing or invalid. Run tools/private_playwright.py setup locally.',file=sys.stderr)
        raise SystemExit(1)
