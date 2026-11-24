"""Schedule engine operating on the editable project model (dicts, JSON-safe).

A project model looks like:
{
  "name": ..., "short_name": ..., "proj_id": ..., "start": "YYYY-MM-DD HH:MM",
  "data_date": ..., "calendar": {"name","hpd","workweek":[0..6],"holidays":[...]},
  "wbs":        [{"id","code","name","parent","seq"}],
  "activities": [{"id","code","name","wbs_id","type","status","dur_h","pct",
                  "cstr_type","cstr_date","depends":[{"id","type","lag_h"}]}],
  "resources":  [{"id","code","name","type"}],
  "assignments":[{"act_id","rsrc_id","qty","cost"}]
}
`schedule(model)` adds start/finish/late_*/float/critical to each activity and
`finish` to the model. It mutates in place and is idempotent.
"""
from __future__ import annotations

from collections import deque
from datetime import date

from .schedule import WorkCalendar, parse_clndr_data, parse_dt, REL_TYPES

# P6 constraint codes we honour (forward/backward)
START_CONSTRAINTS = {"CS_MSO", "CS_SNET", "CS_START"}   # act no earlier than date
FINISH_CONSTRAINTS = {"CS_MFO", "CS_FNLT", "CS_FINISH"}  # act no later than date

FMT = "%Y-%m-%d %H:%M"


def build_calendar(model: dict) -> WorkCalendar:
    c = model.get("calendar") or {}
    week = set(c.get("workweek") or [0, 1, 2, 3, 4])
    hol = set()
    for h in c.get("holidays", []):
        try:
            hol.add(date.fromisoformat(h[:10]))
        except ValueError:
            pass
    hpd = float(c.get("hpd") or 8) or 8.0
    anchor = parse_dt(model.get("start") or "")
    return WorkCalendar(week, hol, hpd, anchor.date() if anchor else None)


def schedule(model: dict) -> dict:
    cal = build_calendar(model)
    acts = {str(a["id"]): a for a in model.get("activities", [])}
    for a in model.get("activities", []):
        a.setdefault("depends", [])
        a.setdefault("dur_h", 0)
    hpd = cal.hpd

    preds = {i: [] for i in acts}
    succs = {i: [] for i in acts}
    dangling = []
    for a in model.get("activities", []):
        aid = str(a["id"])
        for d in a["depends"]:
            pid = str(d.get("id"))
            if pid not in acts:
                dangling.append((aid, pid))
                continue
            ty = d.get("type") or "FS"
            if ty not in ("FS", "SS", "FF", "SF"):
                ty = "FS"
            lag = float(d.get("lag_h") or 0) / hpd
            preds[aid].append((pid, ty, lag))
            succs[pid].append((aid, ty, lag))

    dur = {i: float(acts[i].get("dur_h") or 0) / hpd for i in acts}
    order = [str(a["id"]) for a in model.get("activities", []) if str(a["id"]) in acts]

    # topological order
    indeg = {i: len(preds[i]) for i in acts}
    q = deque(i for i in order if indeg[i] == 0)
    topo = []
    while q:
        n = q.popleft()
        topo.append(n)
        for (s, _t, _l) in succs[n]:
            indeg[s] -= 1
            if indeg[s] == 0:
                q.append(s)
    if len(topo) != len(acts):
        topo += [i for i in order if i not in topo]

    def cidx(a):
        dt = parse_dt(a.get("cstr_date") or "")
        return cal.index_of(dt) if dt else None

    # forward pass
    es = {i: 0.0 for i in acts}
    for n in topo:
        a = acts[n]
        e = es[n]
        for (p, ty, lag) in preds[n]:
            pa = acts[p]
            if ty == "FS":
                cand = es[p] + dur[p] + lag
            elif ty == "SS":
                cand = es[p] + lag
            elif ty == "FF":
                cand = es[p] + dur[p] + lag - dur[n]
            else:
                cand = es[p] + lag - dur[n]
            e = max(e, cand)
        ct = (a.get("cstr_type") or "").upper()
        if ct in START_CONSTRAINTS:
            ci = cidx(a)
            if ci is not None:
                e = max(e, ci)
        es[n] = max(0.0, e)

    pf = max((es[i] + dur[i]) for i in order) if order else 0.0

    # backward pass
    lf = {i: pf for i in order}
    for n in reversed(topo):
        a = acts[n]
        cur = lf[n]
        ct = (a.get("cstr_type") or "").upper()
        if ct in FINISH_CONSTRAINTS:
            ci = cidx(a)
            if ci is not None:
                cur = min(cur, ci)
        for (s, ty, lag) in succs[n]:
            ls_s = lf[s] - dur[s]
            if ty == "FS":
                cand = ls_s - lag
            elif ty == "SS":
                cand = ls_s - lag + dur[n]
            elif ty == "FF":
                cand = lf[s] - lag
            else:
                cand = lf[s] - lag + dur[n]
            cur = min(cur, cand)
        lf[n] = cur

    for n in order:
        a = acts[n]
        a["dur_d"] = round(dur[n], 4)
        a["es"] = round(es[n], 6)
        a["ef"] = round(es[n] + dur[n], 6)
        a["ls"] = round(lf[n] - dur[n], 6)
        a["lf"] = round(lf[n], 6)
        a["tf"] = round(lf[n] - dur[n] - es[n], 6)
        a["total_float_h"] = round(a["tf"] * hpd, 2)
        a["critical"] = abs(a["tf"]) < 1e-6
        a["milestone"] = a.get("type") in ("TT_Mile", "TT_FinMile")
        a["start"] = display_start(cal, a)
        a["finish"] = display_finish(cal, a)
        a["late_start"] = cal.start_dt(a["ls"]).strftime(FMT)
        a["late_finish"] = cal.finish_dt(a["lf"]).strftime(FMT)
    model["finish"] = cal.finish_dt(pf).strftime(FMT)
    model["_dangling"] = dangling
    model["_hpd"] = hpd
    return model


def display_start(cal, a):
    if a.get("type") == "TT_FinMile":
        return cal.finish_dt(a["es"]).strftime(FMT)
    return cal.start_dt(a["es"]).strftime(FMT)


def display_finish(cal, a):
    return cal.finish_dt(a["ef"]).strftime(FMT)
