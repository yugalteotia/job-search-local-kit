# Tracker

The row-level tracker has moved to **`tracker.xlsx`** (sheet "Tracker"; status legend on
sheet "Guide") — one row per requisition. It is the primary shared tracker: both Yugal and
Scout inspect and update it on every state change.

Helper: `tools/update_tracker.py` — `upsert '<json>'`, `get '<company>'`, `list`.

Per-requisition detail still lives in `jobs/<company>-<role-slug>/record.md` (+ `JD.md`,
tailored resume PDF/`.tex`).
