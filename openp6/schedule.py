"""Work calendar + CPM scheduling engine for P6 activities.

Time model: a task's schedule position is measured in *working days* from the
project's working-day index 0 (08:00 on the first working day). Durations and
lags are stored in hours in the XER and converted with the calendar's
hours-per-day. A day index maps to a wall-clock instant:
    start instant of index k  -> 08:00 on the k-th working day
    finish instant of index k -> 17:00 on the (k-1)-th working day
so a 3-day task starting at index 0 runs Mon 08:00 -> Wed 17:00.
"""
from __future__ import annotations

import math
import re
from collections import deque
from datetime import date, datetime, time, timedelta

from .xer import Xer

EXCEL_EPOCH = date(1899, 12, 30)
REL_TYPES = {"PR_FS": "FS", "PR_SS": "SS", "PR_FF": "FF", "PR_SF": "SF"}
# P6 day-of-week index (1..7) -> python weekday (Mon=0)
P6_DOW = {1: 6, 2: 0, 3: 1, 4: 2, 5: 3, 6: 4, 7: 5}


def excel_serial(n) -> date:
    return EXCEL_EPOCH + timedelta(days=int(n))


def parse_clndr_data(data: str):
    """Return (workweek set of python weekdays, holiday set of dates)."""
    if not data:
        return {0, 1, 2, 3, 4}, set()
    week = set()
    for p6day, pyday in P6_DOW.items():
        if f"(0||{p6day}()()" in data:      # explicitly empty block -> non-working
            continue
        if f"(0||{p6day}()(" in data:
            week.add(pyday)
    if not week:
        week = {0, 1, 2, 3, 4}
    holidays = set()
    exc = data.split("Exceptions", 1)[1] if "Exceptions" in data else ""
    for n in re.findall(r"\(d\|(\d+)\)", exc):
        holidays.add(excel_serial(n))
    return week, holidays


def parse_dt(value: str):
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:16], fmt)
        except ValueError:
            continue
    return None


class WorkCalendar:
    def __init__(self, week, holidays, hours_per_day=8.0, anchor=None):
        self.week = week
        self.holidays = holidays
        self.hpd = hours_per_day or 8.0
        self._anchor = anchor or date.today()
        self._build(self._anchor, 4000)

    def _is_work(self, d: date) -> bool:
        return d.weekday() in self.week and d not in self.holidays

    def _build(self, start: date, count: int):
        d = start
        while not self._is_work(d):
            d += timedelta(days=1)
        days = []
        while len(days) < count:
            if self._is_work(d):
                days.append(d)
            d += timedelta(days=1)
        self.days = days

    def _ensure(self, k):
        if k >= len(self.days):
            self._build(self._anchor, k + 2000)

    def index_of(self, dt: datetime) -> float:
        """Wall-clock datetime -> working-day index (float, 0 = 08:00 day 0)."""
        d = dt.date()
        # make sure the calendar covers this date
        need = (d - self._anchor).days + 30
        if need > len(self.days):
            self._ensure(need)
        idx = {dd: i for i, dd in enumerate(self.days)}
        guard = 0
        while d not in idx:
            d += timedelta(days=1)
            guard += 1
            if guard > 4000:
                return 0.0
            if d > self.days[-1]:
                self._ensure(len(self.days) + 1000)
                idx = {dd: i for i, dd in enumerate(self.days)}
        i = idx[d]
        mins = dt.hour * 60 + dt.minute - 8 * 60
        frac = max(0.0, min(mins / (9 * 60), 1.0))
        return i + frac

    def start_dt(self, idx: float) -> datetime:
        idx = max(0.0, idx)
        k = int(math.floor(idx + 1e-9))
        frac = idx - k
        self._ensure(k)
        base = self.days[min(k, len(self.days) - 1)]
        minutes = round(frac * 9 * 60)          # 08:00 -> 17:00 spans 9 wall hours
        return datetime.combine(base, time(8, 0)) + timedelta(minutes=minutes)

    def finish_dt(self, idx: float) -> datetime:
        k = int(math.floor(idx + 1e-9))
        frac = idx - k
        if frac > 1e-9:
            return self.start_dt(idx)
        kk = k - 1
        if kk < 0:
            return self.start_dt(0)
        self._ensure(kk)
        return datetime.combine(self.days[min(kk, len(self.days) - 1)], time(17, 0))

    def working_days_between(self, a: datetime, b: datetime) -> int:
        n = 0
        d = a.date()
        while d <= b.date():
            if self._is_work(d):
                n += 1
            d += timedelta(days=1)
        return n


class Activity:
    def __init__(self, row: dict, sched: "Scheduler"):
        self.sched = sched
        self.row = row
        self.id = row["task_id"]
        self.proj_id = row.get("proj_id", "")
        self.wbs_id = row.get("wbs_id", "")
        self.code = row.get("task_code", "")
        self.name = row.get("task_name", "")
        self.task_type = row.get("task_type", "")
        self.status = row.get("status_code", "")
        hrs = row.get("target_drtn_hr_cnt") or row.get("remain_drtn_hr_cnt") or "0"
        try:
            self.dur_h = float(hrs)
        except ValueError:
            self.dur_h = 0.0
        self.dur = self.dur_h / sched.hpd
        self.is_milestone = self.task_type in ("TT_Mile", "TT_FinMile")
        self.es = self.ef = self.ls = self.lf = self.tf = self.ff = 0.0
        self.critical = False
        self.stored_es = parse_dt(row.get("early_start_date", ""))
        self.stored_ef = parse_dt(row.get("early_end_date", ""))
        self.stored_tf = row.get("total_float_hr_cnt", "")

    # display instants ---------------------------------------------------
    def es_dt(self) -> datetime:
        if self.task_type == "TT_FinMile":
            return self.sched.cal.finish_dt(self.es)
        return self.sched.cal.start_dt(self.es)

    def ef_dt(self) -> datetime:
        if self.is_milestone:
            return self.sched.cal.finish_dt(self.ef)
        return self.sched.cal.finish_dt(self.ef)

    def ls_dt(self) -> datetime:
        return self.sched.cal.start_dt(self.ls)

    def lf_dt(self) -> datetime:
        return self.sched.cal.finish_dt(self.lf)


class Scheduler:
    def __init__(self, xer: Xer, proj_id: str):
        self.xer = xer
        self.proj_id = proj_id
        proj = next(p for p in xer.table("PROJECT") if p["proj_id"] == proj_id)
        self.project = proj

        cal_id = proj.get("clndr_id")
        cal_row = next((c for c in xer.table("CALENDAR")
                        if c.get("clndr_id") == cal_id), None)
        data = cal_row.get("clndr_data", "") if cal_row else ""
        week, hol = parse_clndr_data(data)
        try:
            hpd = float(cal_row.get("day_hr_cnt") or 8) if cal_row else 8.0
        except ValueError:
            hpd = 8.0
        anchor = parse_dt(proj.get("plan_start_date", ""))
        self.hpd = hpd or 8.0
        self.cal = WorkCalendar(week, hol, self.hpd,
                                anchor.date() if anchor else None)
        self.calendar_row = cal_row

        self.acts = {}
        self.order = []
        for t in xer.table("TASK"):
            if t.get("proj_id") != proj_id or not t.get("task_id"):
                continue
            if t["task_id"] in self.acts:
                continue
            a = Activity(t, self)
            self.acts[a.id] = a
            self.order.append(a.id)

        self.preds = {i: [] for i in self.acts}
        self.succs = {i: [] for i in self.acts}
        self.dangling = []
        for r in xer.table("TASKPRED"):
            tid, pid = r.get("task_id"), r.get("pred_task_id")
            if tid not in self.acts:
                continue
            if pid not in self.acts:
                self.dangling.append((tid, pid))
                continue
            ty = REL_TYPES.get(r.get("pred_type", ""), "FS")
            try:
                lag = float(r.get("lag_hr_cnt") or 0) / self.hpd
            except ValueError:
                lag = 0.0
            self.preds[tid].append((pid, ty, lag))
            self.succs[pid].append((tid, ty, lag))

        self._cpm()

    # ---- CPM -----------------------------------------------------------
    def _topo(self):
        indeg = {i: len(self.preds[i]) for i in self.acts}
        q = deque(i for i in self.order if indeg[i] == 0)
        topo = []
        while q:
            n = q.popleft()
            topo.append(n)
            for (s, _ty, _lag) in self.succs[n]:
                indeg[s] -= 1
                if indeg[s] == 0:
                    q.append(s)
        if len(topo) != len(self.acts):     # cycle guard
            topo += [i for i in self.order if i not in topo]
        return topo

    def _cpm(self):
        topo = self._topo()
        es = {i: 0.0 for i in self.order}
        for n in topo:
            a = self.acts[n]
            latest = es[n]
            for (p, ty, lag) in self.preds[n]:
                pa = self.acts[p]
                if ty == "FS":
                    cand = es[p] + pa.dur + lag
                elif ty == "SS":
                    cand = es[p] + lag
                elif ty == "FF":
                    cand = es[p] + pa.dur + lag - a.dur
                else:                       # SF
                    cand = es[p] + lag - a.dur
                latest = max(latest, cand)
            es[n] = latest

        pf = max((es[i] + self.acts[i].dur) for i in self.order) if self.order else 0.0
        self.project_finish = pf

        lf = {i: pf for i in self.order}
        for n in reversed(topo):
            a = self.acts[n]
            latest = lf[n]
            for (s, ty, lag) in self.succs[n]:
                sa = self.acts[s]
                ls_s = lf[s] - sa.dur
                if ty == "FS":
                    cand = ls_s - lag
                elif ty == "SS":
                    cand = ls_s - lag + a.dur
                elif ty == "FF":
                    cand = lf[s] - lag
                else:                       # SF
                    cand = lf[s] - lag + a.dur
                latest = min(latest, cand)
            lf[n] = latest

        for i in self.order:
            a = self.acts[i]
            a.es = es[i]
            a.ef = es[i] + a.dur
            a.lf = lf[i]
            a.ls = lf[i] - a.dur
            a.tf = a.ls - a.es
            a.critical = abs(a.tf) < 1e-6
            a.ff = self._free_float(i, es)

    def _free_float(self, n, es):
        a = self.acts[n]
        best = None
        for (s, ty, lag) in self.succs[n]:
            sa = self.acts[s]
            if ty == "FS":
                cand = es[s] - (es[n] + a.dur + lag)
            elif ty == "SS":
                cand = es[s] - (es[n] + lag)
            elif ty == "FF":
                cand = (es[s] + sa.dur) - (es[n] + a.dur + lag)
            else:
                cand = (es[s] + sa.dur) - (es[n] + lag)
            best = cand if best is None else min(best, cand)
        if best is None:
            best = self.project_finish - (es[n] + a.dur)
        return best

    # ---- helpers -------------------------------------------------------
    def activities(self):
        return [self.acts[i] for i in self.order]

    def critical_path(self):
        return [a for a in self.activities() if a.critical]

    def wbs(self):
        return [w for w in self.xer.table("PROJWBS")
                if w.get("proj_id") == self.proj_id]
