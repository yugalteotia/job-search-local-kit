"""Localhost-only dashboard server (stdlib). Data comes from ui_api; actions go through the shared core."""
import http.server
import json
import mimetypes
import os
import secrets
import socketserver
import stat
import urllib.parse
from pathlib import Path

import progress
import ui_api
from local_paths import ROOT

TOKEN_DIR=Path.home()/'.local/share/job-search-private'
TOKEN_PATH=TOKEN_DIR/'ui-token'
STATIC=ROOT/'ui'
DEFAULT_PORT=8765
MAX_BODY=4096

def load_token():
    """Per-install random token in an owner-only file; never written into the repo."""
    TOKEN_DIR.mkdir(mode=0o700,parents=True,exist_ok=True)
    if not TOKEN_PATH.is_file():
        fd=os.open(TOKEN_PATH,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as file:file.write(secrets.token_urlsafe(32))
    info=TOKEN_PATH.lstat()
    if info.st_uid!=os.getuid() or info.st_mode & 0o077 or stat.S_ISLNK(info.st_mode):
        raise RuntimeError('UI token file must be owner-only')
    return TOKEN_PATH.read_text().strip()

class Handler(http.server.BaseHTTPRequestHandler):
    server_version='JobSearchUI'
    def log_message(self,*args):pass

    # ---- security
    def _host_ok(self):
        port=self.server.server_address[1]
        return self.headers.get('Host') in (f'127.0.0.1:{port}',f'localhost:{port}')
    def _origin_ok(self):
        origin=self.headers.get('Origin')
        return origin is None or origin in (f'http://127.0.0.1:{self.server.server_address[1]}',f'http://localhost:{self.server.server_address[1]}')
    def _token_ok(self):
        given=self.headers.get('X-UI-Token','')
        return secrets.compare_digest(given,self.server.token)
    def _send(self,code,body,ctype='application/json'):
        if isinstance(body,(dict,list)):body=json.dumps(body).encode()
        elif isinstance(body,str):body=body.encode()
        self.send_response(code)
        self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        self.send_header('Referrer-Policy','no-referrer')
        self.end_headers();self.wfile.write(body)
    def _fail(self,code,message):self._send(code,{'error':message})

    def _gate(self,api):
        if not self._host_ok():self._fail(400,'Bad host');return False
        if not self._origin_ok():self._fail(403,'Bad origin');return False
        if api and not self._token_ok():self._fail(401,'Token required');return False
        return True

    # ---- routes
    def do_GET(self):
        url=urllib.parse.urlsplit(self.path);path=url.path;query=urllib.parse.parse_qs(url.query)
        if not path.startswith('/api/'):
            if not self._gate(False):return
            if path=='/favicon.ico':return self._send(204,b'','image/x-icon')
            name={'/':'index.html','/index.html':'index.html','/app.js':'app.js','/style.css':'style.css'}.get(path)
            if not name:return self._fail(404,'Not found')
            ctype={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8'}[Path(name).suffix]
            return self._send(200,(STATIC/name).read_bytes(),ctype)
        if not self._gate(True):return
        try:
            if path=='/api/status':return self._send(200,ui_api.status())
            if path=='/api/handoffs':return self._send(200,ui_api.handoffs())
            if path=='/api/portals':return self._send(200,ui_api.portals())
            if path=='/api/tracker':return self._send(200,ui_api.tracker(query.get('view',['quick'])[0]))
            if path=='/api/progress':
                return self._send(200,progress.read(since=int(query.get('since',['0'])[0])))
            parts=path.split('/')
            if len(parts)==4 and parts[2]=='evidence':return self._send(200,ui_api.evidence_list(parts[3]))
            if len(parts)==5 and parts[2]=='evidence':
                kind,data=ui_api.evidence_file(parts[3],parts[4]);return self._send(200,data,kind)
        except (ValueError,FileNotFoundError) as exc:return self._fail(404,str(exc))
        except Exception as exc:return self._fail(500,type(exc).__name__)
        self._fail(404,'Not found')

    def do_POST(self):
        path=urllib.parse.urlsplit(self.path).path
        if not self._gate(True):return
        length=int(self.headers.get('Content-Length') or 0)
        if length>MAX_BODY:return self._fail(413,'Body too large')
        try:body=json.loads(self.rfile.read(length) or b'{}')
        except ValueError:return self._fail(400,'Invalid JSON')
        try:
            if path=='/api/run':return self._send(200,ui_api.run_now(body.get('dry_run')))
            if path=='/api/pause':return self._send(200,ui_api.pause())
            if path=='/api/resume':return self._send(200,ui_api.resume())
            if path=='/api/portals/source':
                return self._send(200,ui_api.toggle_source(body['section'],body['name'],bool(body['enabled'])))
            if path=='/api/portals/apply':return self._send(200,ui_api.set_lane(body['portal'],bool(body['enabled'])))
            if path=='/api/alerts/ack':return self._send(200,ui_api.ack_alert(str(body['slug'])))
        except (ValueError,KeyError) as exc:return self._fail(409,str(exc))
        except Exception as exc:return self._fail(500,type(exc).__name__)
        self._fail(404,'Not found')

class Server(socketserver.ThreadingMixIn,http.server.HTTPServer):
    daemon_threads=True

def make_server(port=DEFAULT_PORT,token=None):
    server=Server(('127.0.0.1',port),Handler)
    server.token=token or load_token()
    return server

def serve(port=DEFAULT_PORT):
    server=make_server(port)
    print(f'Dashboard: http://127.0.0.1:{server.server_address[1]}/#token={server.token}',flush=True)
    print('Keep this URL private (the token is in the fragment and is never sent to any other host). Ctrl+C to stop.',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
