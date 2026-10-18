"""Policy proxy for private Playwright MCP: one owned tab per worker.

The upstream extension MCP exposes session-scoped numeric tab indexes. This
proxy records the exact index returned by `browser_tabs new`, rejects actions
against unowned tabs, reuses the owned tab on later `new` requests, and closes
only recorded indexes on explicit close or connection shutdown.
"""
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import uuid
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".workflow"
ID_RE = re.compile(r"^-\s*(\d+):\s*\(current\)", re.M)
TAB_LINE_RE = re.compile(r"^-\s*(\d+):\s*(?:\(current\)\s*)?\[([^\]]+)\]\(([^)]+)\)", re.M)
MAX_WORKERS = 3
EXTENSION_URL_RE = re.compile(r"chrome-extension://[^\s\]\[\)\(\"']+", re.I)
TOKEN_PARAMETER_RE = re.compile(r"(?i)\b((?:access_)?token|PLAYWRIGHT_MCP_EXTENSION_TOKEN)=([^&\s\]\[\)\(\"']+)")


def _safe_text(value):
    # MCP appends an "Open tabs" list to many results, including evaluate.
    # Its Welcome-tab URL contains the private extension connection token.
    value = EXTENSION_URL_RE.sub("[extension connection URL redacted]", value)
    return TOKEN_PARAMETER_RE.sub(lambda match: match.group(1) + "=[redacted]", value)


def _safe_response(value):
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, list):
        return [_safe_response(item) for item in value]
    if isinstance(value, dict):
        return {key: _safe_response(item) for key, item in value.items()}
    return value


def _emit(value):
    sys.stdout.write(json.dumps(_safe_response(value)) + "\n")
    sys.stdout.flush()


def log_path():
    run_id = os.environ.get("JOB_SEARCH_RUN_ID", "adhoc-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S"))
    folder = STATE / "runs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{run_id}-tabs.jsonl"


def record(event, **fields):
    payload = {"time": dt.datetime.now(dt.timezone.utc).isoformat(), "event": event, **fields}
    with log_path().open("a", encoding="utf-8") as out:
        out.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _locked_registry(update):
    STATE.mkdir(parents=True, exist_ok=True)
    lock_path = STATE / "active-tab-workers.lock"
    registry_path = STATE / "active-tab-workers.json"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try: rows = json.loads(registry_path.read_text(encoding="utf-8"))
            except (OSError, ValueError): rows = []
            live = []
            for row in rows:
                try:
                    pid=int(row["pid"]);os.kill(pid, 0)
                    stat_line=Path(f"/proc/{pid}/stat").read_text().split()
                    if len(stat_line)>2 and stat_line[2]!='Z':live.append(row)
                except (OSError, KeyError, TypeError, ValueError): pass
            result, live = update(live)
            tmp = registry_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(live, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, registry_path)
            return result
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _reserve(owner):
    def update(rows):
        if len(rows) >= MAX_WORKERS:
            return False, rows
        rows.append({"owner": owner, "pid": os.getpid(), "run_id": os.environ.get("JOB_SEARCH_RUN_ID")})
        return True, rows
    return _locked_registry(update)


def _release(owner):
    return _locked_registry(lambda rows: (None, [row for row in rows if row.get("owner") != owner]))


def _process_tree(root_pid):
    found = {int(root_pid)}
    pending = [int(root_pid)]
    while pending:
        pid = pending.pop()
        children = Path(f"/proc/{pid}/task/{pid}/children")
        try: values = [int(value) for value in children.read_text().split()]
        except (OSError, ValueError): values = []
        for child_pid in values:
            if child_pid not in found:
                found.add(child_pid);pending.append(child_pid)
    return found


def _owned_listener_ports(root_pid):
    socket_inodes = set()
    for pid in _process_tree(root_pid):
        try: descriptors = Path(f"/proc/{pid}/fd").iterdir()
        except OSError: continue
        for descriptor in descriptors:
            try: target = os.readlink(descriptor)
            except OSError: continue
            match = re.fullmatch(r"socket:\[(\d+)\]", target)
            if match: socket_inodes.add(match.group(1))
    ports = set()
    for table in (Path('/proc/net/tcp'), Path('/proc/net/tcp6')):
        try: lines = table.read_text().splitlines()[1:]
        except OSError: continue
        for line in lines:
            columns = line.split()
            if len(columns) > 9 and columns[3] == '0A' and columns[9] in socket_inodes:
                try: ports.add(int(columns[1].rsplit(':', 1)[1], 16))
                except ValueError: pass
    return ports


def _connection_welcome_ids(text, client_name, listener_ports):
    candidates=[]
    for match in TAB_LINE_RE.finditer(text):
        tab_id,title,href=match.groups()
        if title.strip().casefold()!='welcome':continue
        parsed=urlsplit(href)
        if parsed.scheme!='chrome-extension' or not parsed.path.endswith('/connect.html'):continue
        params=parse_qs(parsed.query)
        relay=urlsplit(params.get('mcpRelayUrl',[''])[0])
        try:welcome_client=json.loads(params.get('client',['{}'])[0]).get('name')
        except (ValueError,AttributeError):welcome_client=None
        if relay.scheme not in ('ws','wss') or relay.port not in listener_ports:continue
        if client_name and welcome_client!=client_name:continue
        candidates.append(int(tab_id))
    return candidates


def _tool_error(request_id, message):
    return {"jsonrpc": "2.0", "id": request_id,
            "result": {"isError": True, "content": [{"type": "text", "text": message}]}}


class Relay:
    def __init__(self, child):
        self.child = child
        self.seq = 0

    def call(self, message):
        self.child.stdin.write((json.dumps(message) + "\n").encode())
        self.child.stdin.flush()
        wanted = message.get("id")
        while True:
            ready, _, _ = select.select([self.child.stdout], [], [], 1)
            if not ready:
                if self.child.poll() is not None: raise RuntimeError("Playwright MCP exited")
                continue
            line = self.child.stdout.readline()
            if not line: raise RuntimeError("Playwright MCP closed its output")
            try: response = json.loads(line)
            except ValueError:
                # Unstructured upstream diagnostics are never safe to expose.
                continue
            if response.get("id") == wanted: return response
            # Preserve upstream notifications and server messages.
            _emit(response)

    def internal_tool(self, name, args):
        self.seq += 1
        return self.call({"jsonrpc": "2.0", "id": f"__owned_tab_{self.seq}", "method": "tools/call",
                          "params": {"name": name, "arguments": args}})


def _result_text(response):
    result = response.get("result", {})
    return "\n".join(block.get("text", "") for block in result.get("content", []) if block.get("type") == "text")


def main():
    owner = str(uuid.uuid4())
    reserved = _reserve(owner)
    if not reserved:
        # Refuse before launching the extension backend, which itself opens a
        # Welcome tab during startup.
        for line in sys.stdin.buffer:
            try: request = json.loads(line)
            except ValueError: continue
            if "id" in request:
                response = {"jsonrpc":"2.0","id":request["id"],"error":{"code":-32000,"message":f"Automation worker limit reached ({MAX_WORKERS})"}}
                _emit(response)
        return
    try:
        child = subprocess.Popen(["npx", "-y", "@playwright/mcp@0.0.82", "--extension", "--snapshot-mode", "none"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                                env=os.environ.copy())
    except OSError as exc:
        _release(owner)
        record("playwright_server_start_failed", error=type(exc).__name__, owner=owner)
        for line in sys.stdin.buffer:
            try: request=json.loads(line)
            except ValueError:continue
            if "id" in request:
                response={"jsonrpc":"2.0","id":request["id"],"error":{"code":-32000,"message":f"Playwright launcher unavailable ({type(exc).__name__})"}}
                _emit(response)
        return
    relay = Relay(child)
    owned_ids = []
    created = reused = closed = 0
    unknown_created = 0
    shutting_down = False
    welcome_id = None
    welcome_created = 0
    welcome_closed = 0
    welcome_uncertain = []
    connection_established = False
    client_name = None
    welcome_checked = False

    def stop(_signum, _frame):
        raise SystemExit(128 + _signum)
    signal.signal(signal.SIGTERM, stop)

    try:
        for line in sys.stdin.buffer:
            try: request = json.loads(line)
            except ValueError:continue
            if request.get("method") != "tools/call":
                if "id" not in request:
                    child.stdin.write(line);child.stdin.flush()
                    if request.get("method") == "notifications/initialized" and not welcome_checked:
                        connection_established = True
                        welcome_checked = True
                        try:
                            listing = relay.internal_tool("browser_tabs", {"action":"list"})
                            ports = _owned_listener_ports(child.pid)
                            matches = _connection_welcome_ids(_result_text(listing), client_name, ports)
                            if len(matches) == 1:
                                welcome_id = matches[0];welcome_created = 1
                                record("welcome_tab_created", tab_id=welcome_id, owner=owner,
                                       verified_by="relay_port_owned_by_this_mcp_process")
                            elif len(matches) > 1:
                                welcome_uncertain.extend(matches)
                                record("welcome_tab_ownership_ambiguous", candidate_ids=matches, owner=owner)
                        except Exception as exc:
                            welcome_uncertain.append("welcome_tab_id_unavailable")
                            record("welcome_tab_discovery_failed", owner=owner, error=type(exc).__name__)
                    continue
                if request.get("method") == "initialize":
                    client_name=((request.get("params",{}).get("clientInfo") or {}).get("name"))
                try: response = relay.call(request)
                except Exception as exc:
                    response = {"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32000, "message": type(exc).__name__}}
                _emit(response);continue

            params = request.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {}) or {}
            action = args.get("action") if name == "browser_tabs" else None
            request_id = request.get("id")

            if name == "browser_tabs" and action == "new":
                if owned_ids:
                    # A worker reuses its own dedicated tab; it never grows a tab pile.
                    forwarded = {**request, "params": {**params, "name": "browser_navigate",
                                 "arguments": {"url": args.get("url", "")}}}
                    try: response = relay.call(forwarded);reused += 1
                    except Exception as exc: response = _tool_error(request_id, f"Owned-tab navigation failed: {type(exc).__name__}")
                    record("tab_reused", tab_id=owned_ids[0])
                    _emit(response);continue
                try:
                    response = relay.call(request)
                    match = ID_RE.search(_result_text(response))
                    failed = bool(response.get("error") or response.get("result", {}).get("isError"))
                    if match and not failed:
                        tab_id = int(match.group(1));owned_ids.append(tab_id);created += 1
                        record("tab_created", tab_id=tab_id, owner=owner)
                    elif not failed:
                        unknown_created += 1
                        record("tab_created_untracked", owner=owner, reason="new response omitted tab ID")
                except Exception as exc:
                    response = _tool_error(request_id, f"Tab creation failed: {type(exc).__name__}")
                _emit(response);continue

            if name == "browser_tabs" and action == "list":
                try: response = relay.call(request)
                except Exception as exc: response = _tool_error(request_id, f"Tab listing failed: {type(exc).__name__}")
                _emit(response);continue

            if name == "browser_tabs" and action in ("select", "close"):
                tab_id = args.get("index")
                if tab_id is None or int(tab_id) not in owned_ids:
                    response = _tool_error(request_id, "Blocked: tab ID was not created by this workflow connection")
                    _emit(response);continue
                try:
                    response = relay.call(request)
                    if action == "close" and not response.get("result", {}).get("isError"):
                        owned_ids.remove(int(tab_id));closed += 1
                        record("tab_closed", tab_id=int(tab_id), owner=owner)
                except Exception as exc:
                    response = _tool_error(request_id, f"Owned-tab operation failed: {type(exc).__name__}")
                _emit(response);continue

            if name == "browser_close":
                response = _tool_error(request_id, "Blocked: closing the browser could close personal tabs")
                _emit(response);continue

            page_action = bool(name and name.startswith("browser_") and name != "browser_tabs")
            if page_action and not owned_ids:
                response = _tool_error(request_id, "Blocked: create a workflow-owned tab before page actions")
                _emit(response);continue
            try: response = relay.call(request)
            except Exception as exc: response = _tool_error(request_id, f"Playwright request failed: {type(exc).__name__}")
            _emit(response)
    except (BrokenPipeError, KeyboardInterrupt, SystemExit):
        shutting_down = True
    finally:
        # Close only exact indexes recorded from this connection's startup
        # relay handshake or its `new` responses. Descending order preserves
        # any lower recorded indexes until their own close attempt.
        closables=[("job", tab_id) for tab_id in owned_ids]
        if connection_established and welcome_id is not None:
            closables.append(("welcome", welcome_id))
        for kind, tab_id in sorted(closables, key=lambda pair: pair[1], reverse=True):
            try:
                response = relay.internal_tool("browser_tabs", {"action": "close", "index": tab_id})
                if not response.get("result", {}).get("isError"):
                    if kind == "job":
                        owned_ids.remove(tab_id);closed += 1
                        record("tab_closed", tab_id=tab_id, owner=owner, automatic=True, tab_kind="job")
                    else:
                        welcome_id=None;welcome_closed += 1
                        record("welcome_tab_closed", tab_id=tab_id, owner=owner, automatic=True)
                else:
                    if kind=="welcome":welcome_uncertain.append(tab_id)
                    record("tab_close_uncertain", tab_id=tab_id, owner=owner, tab_kind=kind)
            except Exception as exc:
                if kind=="welcome":welcome_uncertain.append(tab_id)
                record("tab_close_uncertain", tab_id=tab_id, owner=owner, tab_kind=kind, error=type(exc).__name__)
        if reserved: _release(owner)
        shutting_down = True
        remaining_welcome_ids=set(welcome_uncertain)
        if welcome_id is not None:remaining_welcome_ids.add(welcome_id)
        remaining_welcome = len(remaining_welcome_ids)
        summary = {"created": created + welcome_created, "reused": reused,
                   "closed": closed + welcome_closed,
                   "job_tabs_created": created, "job_tabs_closed": closed,
                   "welcome_tabs_created": welcome_created, "welcome_tabs_closed": welcome_closed,
                   "remaining_job_tabs": len(owned_ids) + unknown_created,
                   "remaining_welcome_tabs": remaining_welcome,
                   "remaining_automation_tabs": len(owned_ids) + unknown_created + remaining_welcome,
                   "remaining_tab_ids": owned_ids + (["unknown_job_tab"] * unknown_created) +
                                        list(remaining_welcome_ids),
                   "owner": owner, "automatic_cleanup": shutting_down}
        record("tab_lifecycle_summary", **summary)
        if child.poll() is None:
            child.terminate()
            try: child.wait(timeout=3)
            except subprocess.TimeoutExpired: child.kill();child.wait()


if __name__ == "__main__":
    try: main()
    except Exception as exc:
        print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "error", "data": type(exc).__name__}}), flush=True)
