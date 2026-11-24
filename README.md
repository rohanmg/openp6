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
Resources / Relationships / Codes / Notes).

```bash
./start.sh                       # binds 0.0.0.0:8080
# or:  OPENP6_PASSWORD=secret .venv/bin/python web/server.py
```

* Local:  http://localhost:8080
* LAN:    http://<this-machine-ip>:8080   (phone on the same Wi-Fi)
* Login password lives in `./.password` (or `$OPENP6_PASSWORD`).
* `OPENP6_LOCAL_TRUST=1` skips the password for localhost only.

## What it does

| Step | Detail |
|---|---|
| Parse | Lossless XER reader (Windows-1252, CRLF, all tables). Write-back supported by `Xer.write()`. |
| Calendars | Reads P6 `clndr_data`: working week + holiday exceptions, hours/day. |
| CPM | Forward pass (ES/EF) and backward pass (LS/LF) with `PR_FS/PR_SS/PR_FF/PR_SF` and hour lags; total & free float; critical path. |
| Milestones | Start/finish milestones placed on the correct 08:00 / 17:00 instant. |
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
