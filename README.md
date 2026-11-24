# openP6

A free, open-source critical-path (CPM) scheduler that reads **Primavera P6
`.xer`** exports — no P6 licence, no paid tooling, no dependencies.

It parses the XER, recomputes the schedule with a full forward/backward pass
(FS/SS/FF/SF relationships, lags, work calendars with holidays, milestones),
and renders a Gantt chart plus a reusable activity table.

```
pip install -e .          # or just run from the source tree (stdlib only)
python -m openp6 projects schedule.xer
python -m openp6 report   schedule.xer --project BRIDGE
python -m openp6 gantt    schedule.xer --out gantt.html
python -m openp6 csv      schedule.xer --out activities.csv
```

## Web app (P6-style UI)

A P6-Professional-style web workspace: menu bar, toolbar, EPS/projects
navigator, WBS-grouped activity table, a synchronised Gantt with critical-path
highlighting and total-float bars, and a Details pane (General / Status /
Resources / Relationships / Codes / Project).

**It edits, not just views.** Change an activity's ID, name, duration, %
complete, type, WBS and constraint; add or delete activities; add/remove
predecessors (FS/SS/FF/SF + lag, circular links rejected); edit the project and
calendar; then recompute with **Schedule** (F9) and **Export .xer** to take the
schedule back into Primavera P6.

See **[DEPLOY.md](DEPLOY.md)** for free always-on hosting (Render, Northflank,
Oracle Cloud) and the tunnel option.

```bash
./start.sh                       # binds 0.0.0.0:8080
# or:  OPENP6_PASSWORD=secret DATABASE_URL=... .venv/bin/python web/server.py
```

* Local:  http://localhost:8080
* LAN:    http://<this-machine-ip>:8080   (phone on the same Wi-Fi)
* Login password lives in `./.password` (or `$OPENP6_PASSWORD`).
* `OPENP6_LOCAL_TRUST=1` skips the password for localhost only (never enable
  this when a tunnel or proxy sits in front — the proxy looks like localhost).
* `DATABASE_URL` switches storage from local JSON files to Postgres, so the app
  survives redeploys on hosts with an ephemeral filesystem.

## What it does

| Step | Detail |
|---|---|
| Parse | Lossless XER reader; auto-detects **UTF-8 vs Windows-1252** so accented names don't turn to mojibake. Write-back via `Xer.write()`. |
| Calendars | Reads P6 `clndr_data`: working week + holiday exceptions, hours/day. |
| CPM | Forward pass (ES/EF) and backward pass (LS/LF) with `PR_FS/PR_SS/PR_FF/PR_SF` and hour lags; total & free float; critical path. |
| Milestones | Start/finish milestones placed on the correct 08:00 / 17:00 instant. |
| Scale | **Virtualised grid** — only on-screen rows get DOM nodes (40,000 activities → ~40 rows); gzip + response cache keep the payload ~1 MB. |
| Robustness | Undo/redo history (40 revisions), optimistic-locking conflict detection across devices, input validation, atomic crash-safe writes, `/api/backup`. |
| Output | HTML Gantt (self-contained), CSV, console report. |

## Verification

`verify.py` re-schedules an XER and compares the computed early dates and total
float against the values stored in the file.

```
python verify.py sample.xer sample_multi.xer
```

* `sample.xer` (Drawworks corpus) — **6/6 exact**: every ES, EF and total-float
  value matches, including an SS+lag chain, a holiday that shifts a finish to
  the next working day, a 32h-float off-path task and a 17:00 finish milestone.
* `sample_multi.xer` (mpptool corpus) — all early dates match; one stored
  total-float value disagrees with the same file's own other fields (that
  fixture also stores a `-1` float placeholder), so it is not treated as
  authoritative.

## Limitations / roadmap

- Multi-project *inter-project* relationships are not yet chained.
- Constraints (MSO/MEO etc.), resource levelling and assignable calendars per
  activity are not modelled yet — dates on constrained tasks can differ.
- No XER **write-back of edits** yet (reader round-trips; editing is next).
- Interoperability cross-check against MPXJ is planned.

## Licence

MIT. Built on the public XER format; test fixtures from the Drawworks and
mpptool projects.
