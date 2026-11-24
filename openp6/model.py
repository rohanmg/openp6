"""Convert between a Primavera P6 .xer file and the editable project model."""
from __future__ import annotations

import re
from datetime import date, timedelta

from .xer import Xer
from .schedule import parse_clndr_data, REL_TYPES

EXCEL_EPOCH = date(1899, 12, 30)
P6_DOW = {0: 2, 1: 3, 2: 4, 3: 5, 4: 6, 5: 7, 6: 1}   # python weekday -> P6 day (1=Sun)
P6_DOW_PY = {v: k for k, v in P6_DOW.items()}          # P6 day -> python weekday


def from_xer(xer: Xer, proj_id: str) -> dict:
    proj = next(p for p in xer.table("PROJECT") if p["proj_id"] == proj_id)
    cal_row = next((c for c in xer.table("CALENDAR")
                    if c.get("clndr_id") == proj.get("clndr_id")), None)
    week, hol = parse_clndr_data((cal_row or {}).get("clndr_data", ""))
    try:
        hpd = float((cal_row or {}).get("day_hr_cnt") or 8)
    except ValueError:
        hpd = 8.0

    wbs = [{"id": w["wbs_id"], "code": w.get("wbs_short_name", ""),
            "name": w.get("wbs_name", ""), "parent": w.get("parent_wbs_id", ""),
            "seq": float(w.get("seq_num") or 0)}
           for w in xer.table("PROJWBS") if w.get("proj_id") == proj_id]

    task_ids = {t["task_id"] for t in xer.table("TASK") if t.get("proj_id") == proj_id}
    deps: dict[str, list] = {}
    for r in xer.table("TASKPRED"):
        if r.get("task_id") in task_ids and r.get("pred_task_id") in task_ids:
            deps.setdefault(r["task_id"], []).append({
                "id": r["pred_task_id"],
                "type": REL_TYPES.get(r.get("pred_type", ""), "FS"),
                "lag_h": _f(r.get("lag_hr_cnt")),
            })

    acts = []
    for t in xer.table("TASK"):
        if t.get("proj_id") != proj_id:
            continue
        acts.append({
            "id": t["task_id"], "code": t.get("task_code", ""),
            "name": t.get("task_name", ""), "wbs_id": t.get("wbs_id", ""),
            "type": t.get("task_type", "TT_Task"),
            "status": t.get("status_code", "TK_NotStart"),
            "dur_h": _f(t.get("target_drtn_hr_cnt") or t.get("remain_drtn_hr_cnt")),
            "pct": _f(t.get("phys_complete_pct")),
            "cstr_type": t.get("cstr_type", ""), "cstr_date": t.get("cstr_date", ""),
            "depends": deps.get(t["task_id"], []),
        })

    resources = [{"id": r["rsrc_id"], "code": r.get("rsrc_short_name", ""),
                  "name": r.get("rsrc_name", ""), "type": r.get("rsrc_type", "")}
                 for r in xer.table("RSRC")]
    assignments = [{"act_id": a.get("task_id"), "rsrc_id": a.get("rsrc_id"),
                    "qty": _f(a.get("target_qty")), "cost": _f(a.get("target_cost"))}
                   for a in xer.table("TASKRSRC")]

    return {
        "proj_id": proj_id,
        "short_name": proj.get("proj_short_name", ""),
        "name": proj.get("proj_name") or proj.get("proj_short_name", ""),
        "start": proj.get("plan_start_date", ""),
        "data_date": proj.get("last_recalc_date", ""),
        "calendar": {"name": (cal_row or {}).get("clndr_name", "Standard 5 Day"),
                     "hpd": hpd, "workweek": sorted(week),
                     "holidays": [d.isoformat() for d in sorted(hol)]},
        "wbs": wbs, "activities": acts, "resources": resources,
        "assignments": assignments,
    }


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def build_clndr_data(cal: dict) -> str:
    hpd = int(cal.get("hpd") or 8)
    week = set(cal.get("workweek") or [0, 1, 2, 3, 4])
    days = []
    for p6 in range(1, 8):                    # P6 lists Sunday..Saturday (1..7)
        pyd = P6_DOW_PY[p6]
        if pyd in week:
            days.append(f"(0||{p6}()((0||0(s|08:00|f|12:00)())(0||1(s|13:00|f|17:00)())))")
        else:
            days.append(f"(0||{p6}()())")
    exc = []
    for i, h in enumerate(cal.get("holidays", [])):
        try:
            d = date.fromisoformat(h[:10])
        except ValueError:
            continue
        serial = (d - EXCEL_EPOCH).days
        exc.append(f"(0||{i}(d|{serial})())")
    return ("(0||CalendarData()((0||DaysOfWeek()(" + "".join(days) +
            "))(0||VIEW(ShowTotal|Y)())(0||Exceptions()(" + "".join(exc) + "))))")


def to_xer(model: dict) -> Xer:
    """Rebuild an XER file from the model (write-back / round-trip)."""
    cal = model.get("calendar") or {}
    hpd = int(cal.get("hpd") or 8)
    xer = Xer(header=["ERMHDR", "19.12", date.today().isoformat(), "Project",
                      "admin", "openP6", "dbxDatabaseNoName", "Project Management",
                      "USD"], fields={}, rows={})

    def table(name, fields, rows):
        xer.fields[name] = fields
        xer.rows[name] = rows

    table("CURRTYPE", ["curr_id", "curr_short_name"], [{"curr_id": "1", "curr_short_name": "USD"}])
    table("CALENDAR",
          ["clndr_id", "default_flag", "clndr_name", "proj_id", "base_clndr_id",
           "clndr_type", "day_hr_cnt", "week_hr_cnt", "clndr_data"],
          [{"clndr_id": "1", "default_flag": "Y", "clndr_name": cal.get("name") or "Standard",
            "proj_id": model.get("proj_id", "1"), "base_clndr_id": "", "clndr_type": "CA_Base",
            "day_hr_cnt": str(hpd), "week_hr_cnt": str(hpd * 5),
            "clndr_data": build_clndr_data(cal)}])
    table("PROJECT",
          ["proj_id", "proj_short_name", "clndr_id", "plan_start_date",
           "scd_end_date", "last_recalc_date"],
          [{"proj_id": model.get("proj_id", "1"),
            "proj_short_name": model.get("short_name", "PROJECT"), "clndr_id": "1",
            "plan_start_date": model.get("start", ""),
            "scd_end_date": model.get("finish", ""),
            "last_recalc_date": model.get("data_date") or model.get("start", "")}])
    table("PROJWBS",
          ["wbs_id", "proj_id", "seq_num", "proj_node_flag", "wbs_short_name",
           "wbs_name", "parent_wbs_id"],
          [{"wbs_id": w["id"], "proj_id": model.get("proj_id", "1"),
            "seq_num": str(int(w.get("seq") or 0)),
            "proj_node_flag": "Y" if not w.get("parent") else "N",
            "wbs_short_name": w.get("code", ""), "wbs_name": w.get("name", ""),
            "parent_wbs_id": w.get("parent", "")} for w in model.get("wbs", [])])

    tasks, preds, pid = [], [], 0
    for a in model.get("activities", []):
        tasks.append({
            "task_id": str(a["id"]), "proj_id": model.get("proj_id", "1"),
            "wbs_id": a.get("wbs_id", ""), "clndr_id": "1",
            "phys_complete_pct": str(a.get("pct") or 0),
            "task_type": a.get("type", "TT_Task"),
            "status_code": a.get("status", "TK_NotStart"),
            "task_code": a.get("code", ""), "task_name": a.get("name", ""),
            "total_float_hr_cnt": str(a.get("total_float_h", 0)),
            "target_drtn_hr_cnt": str(a.get("dur_h") or 0),
            "remain_drtn_hr_cnt": str(a.get("dur_h") or 0),
            "cstr_type": a.get("cstr_type", ""), "cstr_date": a.get("cstr_date", ""),
            "early_start_date": a.get("start", ""), "early_end_date": a.get("finish", ""),
            "late_start_date": a.get("late_start", ""), "late_end_date": a.get("late_finish", ""),
        })
        for d in a.get("depends", []):
            pid += 1
            preds.append({"task_pred_id": str(pid), "task_id": str(a["id"]),
                          "pred_task_id": str(d["id"]), "proj_id": model.get("proj_id", "1"),
                          "pred_proj_id": model.get("proj_id", "1"),
                          "pred_type": "PR_" + (d.get("type") or "FS"),
                          "lag_hr_cnt": str(d.get("lag_h") or 0)})
    table("TASK", list(tasks[0].keys()) if tasks else
          ["task_id", "proj_id", "wbs_id", "clndr_id", "phys_complete_pct", "task_type",
           "status_code", "task_code", "task_name", "total_float_hr_cnt",
           "target_drtn_hr_cnt", "remain_drtn_hr_cnt", "cstr_type", "cstr_date",
           "early_start_date", "early_end_date", "late_start_date", "late_end_date"], tasks)
    table("TASKPRED", ["task_pred_id", "task_id", "pred_task_id", "proj_id",
                       "pred_proj_id", "pred_type", "lag_hr_cnt"], preds)
    table("RSRC", ["rsrc_id", "rsrc_short_name", "rsrc_name", "rsrc_type"],
          [{"rsrc_id": r["id"], "rsrc_short_name": r.get("code", ""),
            "rsrc_name": r.get("name", ""), "rsrc_type": r.get("type", "")}
           for r in model.get("resources", [])])
    table("TASKRSRC", ["taskrsrc_id", "task_id", "rsrc_id", "proj_id", "target_qty",
                       "target_cost"],
          [{"taskrsrc_id": str(i + 1), "task_id": str(a.get("act_id")),
            "rsrc_id": str(a.get("rsrc_id")), "proj_id": model.get("proj_id", "1"),
            "target_qty": str(a.get("qty") or 0), "target_cost": str(a.get("cost") or 0)}
           for i, a in enumerate(model.get("assignments", []))])
    return xer


def new_activity_id(model: dict) -> str:
    used = {str(a["id"]) for a in model.get("activities", [])}
    n = 1
    while str(n) in used:
        n += 1
    return str(n)
