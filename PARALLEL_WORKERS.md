# Bounded parallel workflow

The four existing timer slots still launch `workflow daily --scheduled --max-seconds 1800`.
The normal runner checks the same private Chrome gate, pause control and `run.lock`.
`.workflow/parallel-config.json` selects the parallel path. The timer, missed-run
rule and submission proof gates are unchanged.

Three independent Codex/Playwright MCP connections search separate source shards
concurrently. Each shard has one browser connection and creates its own tab. The
shards write separate JSONL logs, structured search-page captures and digests.
Each reachable portal should receive two targeted Java/Spring Boot/backend/API
queries, with up to two actual result pages per query and date sorting where
available. The capture records pages opened and unique original requisitions,
never a search-result count as coverage. Indeed HTTP 403 remains blocked; no bypass.

Verified High-fit listings with employer requisition IDs enter
`.workflow/priority-queue.sqlite3`. SQLite uses `BEGIN IMMEDIATE`, a unique
company/requisition constraint, priority plus observed recency, and one active
interaction per employer or ATS host. Three resume workers may tailor and visually
review different job folders at once. A terminal or uncertain existing application
is never queued for reapplication. Claims left active after a crash are not silently
recycled; they require inspection. A blocked application releases its queue slot
without changing another job. Site errors can put an ATS host on cooldown.

Browser form and submission concurrency is **one**. Three simultaneous read-only
Chrome tabs kept distinct page identities. Two simultaneous local dummy forms kept
their job IDs and text fields separate, but neither could finish PDF attachment:
one file-picker click timed out, and the extension returned `DOM.setFileInputFiles`
`Not allowed` (−32000). A single dummy form produced the same upload error. Thus
two complete independent form preparations did **not** pass, and parallel
submissions are disabled. A real form needing this upload becomes a per-job handoff;
do not claim it submitted. This is a Chrome extension protocol limitation, not
permission to fabricate attachment proof. Re-run the no-submit tests before raising
the form cap, and require successful attachment verification for two different jobs.
Playwright MCP's form tools have no tab-ID parameter: they act on the selected tab
of their connection. Separate connections passed the read-only identity test, but
the failed preparation test does not establish safe concurrent form actions or
uploads. The one-worker form cap is intentional until that boundary is proven.

Each job worker gets a dedicated process and private MCP connection, plus an exact
slug, normalized employer/requisition and original URL binding in its environment.
`workflow` rejects worker writes to another job folder, requisition, or direct URL.
The worker is instructed to create a dedicated tab and verify the requisition and
ATS continuation before every form action. The single form worker is the current
safe cap; separate MCP connection state is proven for read-only navigation, while
form attachment isolation is not. `begin-submission` also writes a persistent
atomic `O_EXCL` per-requisition claim before any final click. A crash or uncertain
submission leaves that claim in place and blocks retry. `Applied` still requires
actual confirmation proof.

Commands:

```bash
python3 workflow status
python3 workflow queue-status
python3 workflow daily --max-seconds 1800
python3 tools/parallel_browser_probe.py
python3 tools/parallel_browser_probe.py --forms
python3 workflow logs
```

The normal `workflow pause` and `workflow resume` controls apply. Worker logs are
`.workflow/runs/<run-id>-<shard-or-worker>.jsonl`; stage metrics and coverage paths
are in `.workflow/digests/<run-id>.md`. Log records contain tool names/status and
stage timing, not browser arguments, form answers, cookies or tokens.
