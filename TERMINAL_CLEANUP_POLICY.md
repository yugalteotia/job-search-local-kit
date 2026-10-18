# Terminal role cleanup

`workflow cleanup-preview` lists every current deletion candidate with path,
byte count and SHA256. The first cleanup is applied only after that preview has
been shown. `workflow cleanup-apply` checks that the tracker rows and every file
still match the preview before changing anything. It backs up `tracker.xlsx`,
reduces explicitly Rejected, Closed, Expired or Withdrawn rows (including
`Excluded — closed/expired`) to seven fields, verifies their deduplication keys,
then deletes only the previewed files. `Excluded — stale` is **not** treated as
expired without evidence. Unknown dates and reasons remain `unknown` and
`reason unknown`; a submission date is never substituted for a rejection date.

The seven retained fields are Company, Role, Job ID, Direct link, Status, Status
date and Known reason. No further JD, location, salary, resume or response detail
is added to a terminal tracker row. Earlier backups and global change/audit files
remain solely as recovery history; they are not active tracker rows. Terminal
requisitions remain blocked at discovery, tailoring and submission gates. A
shared capture is retained if it also documents a nonterminal/historical role.

Terminal-only job JDs, listing packets, working notes, unused PDFs, PNG renders,
screenshots and captures are deleted. Submission and portal-status confirmation
artifacts are the sole exception: retain for **30 × 24 hours** from the recorded
confirmation timestamp, then prune on a later normal run. If the timestamp is
missing or invalid, retain and flag for manual review instead of guessing an age.
`workflow cleanup-prune` checks the saved file hash before deleting proof. The
retention manifest lives in `.workflow/cleanup/retention.json`. Shared historical
application evidence is reduced to minimal deduplication keys before proof is
pruned; importing historical tracker rows still requires the separate preview
and approval process.

The original first preview is archived at `.workflow/cleanup/first-preview.md`
and `.json`; current/future previews use `.workflow/cleanup/preview.md` and
`.json`. Normal daily runs may
automatically preview and apply future terminal rows after the initial cleanup;
the per-run log records the exact files removed. The cleanup never removes the
minimal history key or treats a missing requisition ID as proof of a new role.
