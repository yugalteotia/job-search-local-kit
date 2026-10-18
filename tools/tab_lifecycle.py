"""Track and close only tabs created by a workflow MCP connection.

The Playwright MCP extension exposes session-scoped numeric tab indexes, not
Chrome target IDs. We record the index returned by each `browser_tabs new` call
and close only that recorded index on the same MCP connection. If it cannot be
parsed, the tab is left open and reported as uncertain.
"""
import datetime as dt
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_CURRENT = re.compile(r"^-\s*(\d+):\s*\(current\)", re.M)


def _text(result):
    return "\n".join(item.get("text", "") for item in result.get("content", [])
                     if item.get("type") == "text")


class OwnedTabs:
    """A per-MCP-session tab pool, capped at three owned tabs."""
    def __init__(self, client, run_id=None, owner="workflow", cap=3):
        self.client = client
        self.run_id = run_id or os.environ.get("JOB_SEARCH_RUN_ID") or dt.datetime.now(dt.timezone.utc).strftime("adhoc-%Y%m%dT%H%M%S")
        self.owner = owner
        self.cap = max(1, min(3, int(cap)))
        self.ids = []
        self.created = 0
        self.reused = 0
        self.closed = 0
        self.uncertain = []
        self.unknown_created = 0
        self.log = ROOT / ".workflow" / "runs" / f"{self.run_id}-tabs-helper.jsonl"

    def _record(self, event, **fields):
        self.log.parent.mkdir(parents=True, exist_ok=True)
        payload = {"time": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "event": event, "owner": self.owner, **fields}
        with self.log.open("a", encoding="utf-8") as out:
            out.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def open(self, url):
        if len(self.ids) >= self.cap:
            raise RuntimeError(f"Owned tab cap ({self.cap}) reached")
        result = self.client.tool("browser_tabs", {"action": "new", "url": url}, timeout=40)
        match = _CURRENT.search(_text(result))
        if not match:
            self.unknown_created += 1
            self.uncertain.append("new tab response did not expose a session tab ID")
            self._record("tab_created_untracked", reason="missing_tab_id")
            raise RuntimeError("Playwright did not return a tab ID; leaving the tab open safely")
        tab_id = int(match.group(1))
        if tab_id in self.ids:
            self.uncertain.append(f"duplicate returned tab ID {tab_id}")
            raise RuntimeError("Playwright returned a duplicate tab ID")
        self.ids.append(tab_id)
        self.created += 1
        self._record("tab_created", tab_id=tab_id)
        return tab_id

    def reuse(self, tab_id, url):
        if tab_id not in self.ids:
            raise ValueError("Refusing to select a tab not created by this workflow")
        self.client.tool("browser_tabs", {"action": "select", "index": tab_id}, timeout=20)
        self.client.tool("browser_navigate", {"url": url}, timeout=40)
        self.reused += 1
        self._record("tab_reused", tab_id=tab_id)

    def note_reuse(self, tab_id):
        if tab_id not in self.ids:
            raise ValueError("Refusing to count reuse of an unowned tab")
        self.reused += 1
        self._record("tab_reused", tab_id=tab_id)

    def close_all(self):
        # Closing in descending order avoids renumbering another owned index
        # before its close. Any MCP error leaves that tab explicitly reported.
        for tab_id in sorted(self.ids, reverse=True):
            try:
                self.client.tool("browser_tabs", {"action": "close", "index": tab_id}, timeout=20)
                self.closed += 1
                self._record("tab_closed", tab_id=tab_id)
            except Exception as exc:
                self.uncertain.append(f"tab ID {tab_id} could not be closed ({type(exc).__name__})")
                self._record("tab_close_uncertain", tab_id=tab_id, error=type(exc).__name__)
        remaining = len(self.ids) - self.closed + self.unknown_created
        summary = {"created": self.created, "reused": self.reused, "closed": self.closed,
                   "remaining_automation_tabs": remaining, "remaining_tab_ids": self.uncertain.copy()}
        self._record("tab_lifecycle_summary", **summary)
        return summary
