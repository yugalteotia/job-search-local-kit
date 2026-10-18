# Local Chrome connection setup

The initial separate Codex CLI probe failed: the user observed Playwright's
manual **Allow & select** dialog and did not approve it. The probe reached
`browser_evaluate` but did not finish within 180 seconds; its process group was
terminated. See `.workflow/browser-probe.json`. After private setup, the separate
no-click Gmail account-control test passed: `.workflow/browser-probe-private.json`.
The timer is configured for 08:30, 12:30, 16:30 and 20:30 Asia/Kolkata; workflow status is authoritative.

## One-time private setup

1. Open the Playwright extension's status page in the signed-in Chrome profile.
   Click **Generate new token**. This replaces the token used for future
   connections. Disconnect obsolete clients separately on that page; regeneration
   does not establish that an already connected client has been disconnected.
2. Run `python3 tools/private_playwright.py setup` in your own terminal.
   Paste the new token at the hidden prompt, never in chat or a shell command.
   Enter the last directory component of Profile Path at `chrome://version`.
3. Clear the clipboard and any clipboard-history entry containing the token.
4. Run `python3 tools/browser_probe.py --private` without approving any browser
   dialog. A dialog still appearing means the unattended test failed.

The helper stores the credential only in
`~/.config/job-search-private/playwright.json` (0600), inside a 0700 directory.
It pins Playwright MCP 0.0.82, selects the specified Chrome profile, and injects
the token into the MCP child environment. It does not put it into Codex prompts,
command arguments, project configuration, or logs. Raw Codex tool output stays in
memory and is discarded; the probe persists an allowlisted result. The file is
permission-protected, not encrypted; other processes running as your OS user or
root can access it. Keep this directory out of backups/sync and use laptop disk
encryption. Anyone holding the token can authorize browser access, so regenerate
it after suspected exposure and rerun the local setup to replace the stored copy.

The helper does not change the interactive Codex MCP configuration. Each client
has its own Chrome tab group. The retest opens the Taleo submissions list read-only
and checks signed-in identity; it never reopens Optum's application or retries it.
Passing that test proves that connection at that time, not that every portal,
upload, login, declaration or CAPTCHA can run without a handoff.

Chrome and the laptop must remain running and awake for browser work. A separate
CLI process does not need the Codex app window. Sleep suspends CPU/network work;
closing Chrome ends browser access. The timer does not wake the laptop and skips
missed runs outside each slot's :30–:44 IST window. See WORKFLOW_README.md for commands.

Official setup: https://github.com/microsoft/playwright/tree/main/packages/extension#readme
Token regeneration implementation: https://github.com/microsoft/playwright/blob/main/packages/extension/src/ui/authToken.tsx
