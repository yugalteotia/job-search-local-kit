#!/usr/bin/env python3
"""Incremental updates to ~/workspace/job-search/tracker.xlsx (sheet "Tracker").

Usage:
  update_tracker.py upsert '<json-object>'     # match by Company+Job ID+Role, update or append
  update_tracker.py get '<company-substring>'  # print matching rows as JSON
  update_tracker.py list                       # print all rows (Company, Role, Status)

JSON keys use the exact column names from the header row.
"""
import json
import sys
from local_paths import ROOT
import openpyxl

PATH = str(ROOT / "tracker.xlsx")
KEYS = ("Company", "Job ID", "Role")

def load():
    wb = openpyxl.load_workbook(PATH)
    ws = wb["Tracker"]
    cols = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
    return wb, ws, cols

def rows(ws, cols):
    out = []
    for r in range(2, ws.max_row + 1):
        d = {cols[c]: ws.cell(row=r, column=c + 1).value for c in range(len(cols))}
        d["_row"] = r
        out.append(d)
    return out

def upsert(data):
    from workflow import upsert as audited_upsert
    print('row ' + str(audited_upsert(data, 'legacy helper upsert')))
    return
    wb, ws, cols = load()
    match = None
    for row in rows(ws, cols):
        if all((row.get(k) or "") == (data.get(k) or "") for k in KEYS):
            match = row["_row"]
            break
    if match:
        r = match
    else:
        r = ws.max_row + 1
    for c, name in enumerate(cols, 1):
        if name in data:
            # NOTE: keep URLs as plain text — do NOT set cell.hyperlink;
            # hyperlink objects render as "[object Object]" in the viewer.
            ws.cell(row=r, column=c, value=data[name])
    wb.save(PATH)
    print(("updated row " if match else "appended row ") + str(r))

def main():
    cmd = sys.argv[1]
    if cmd == "upsert":
        upsert(json.loads(sys.argv[2]))
    elif cmd == "get":
        wb, ws, cols = load()
        q = sys.argv[2].lower()
        out = [r for r in rows(ws, cols) if q in str(r.get("Company", "")).lower()]
        print(json.dumps(out, indent=1, default=str))
    elif cmd == "list":
        wb, ws, cols = load()
        for r in rows(ws, cols):
            print(f"{r.get('Company')} | {r.get('Role')} | {r.get('Status')}")
    else:
        sys.exit("unknown command")

if __name__ == "__main__":
    main()
