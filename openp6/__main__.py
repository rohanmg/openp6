"""openP6 command line:

    python -m openp6 projects FILE                 list projects in an XER
    python -m openp6 report   FILE [--project P]   print a schedule table
    python -m openp6 gantt    FILE [--out OUT]     write an HTML Gantt chart
    python -m openp6 csv      FILE [--out OUT]     write an activity CSV
"""
from __future__ import annotations

import argparse
import csv
import io
import sys

from .xer import Xer
from .schedule import Scheduler
from .render import render_html

FMT = "%Y-%m-%d %H:%M"


def _load(path):
    return Xer.load(path)


def cmd_projects(args):
    xer = _load(args.file)
    for p in xer.table("PROJECT"):
        print(f"{p['proj_id']:>8}  {p.get('proj_short_name',''):<12} "
              f"start={p.get('plan_start_date',''):<18} "
              f"{p.get('proj_name','')}")
    return 0


def _pick(xer, project):
    projects = xer.table("PROJECT")
    if not projects:
        raise SystemExit("no PROJECT table found")
    if project is None:
        return projects[0]["proj_id"]
    for p in projects:
        if project in (p["proj_id"], p.get("proj_short_name")):
            return p["proj_id"]
    raise SystemExit(f"project {project!r} not found")


def cmd_report(args):
    xer = _load(args.file)
    s = Scheduler(xer, _pick(xer, args.project))
    if s.dangling:
        print(f"! {len(s.dangling)} dangling relationship(s): {s.dangling}",
              file=sys.stderr)
    hdr = f"{'code':<9}{'name':<30}{'start':<18}{'finish':<18}{'TF h':>7}  C"
    print(hdr)
    print("-" * len(hdr))
    for a in s.activities():
        print(f"{a.code:<9}{a.name[:29]:<30}{a.es_dt().strftime(FMT):<18}"
              f"{a.ef_dt().strftime(FMT):<18}{a.tf * s.hpd:>7.0f}  "
              f"{'*' if a.critical else ''}")
    print(f"\nproject finish {s.cal.finish_dt(s.project_finish).strftime(FMT)}  "
          f"| critical: {len(s.critical_path())}/{len(s.acts)}")
    return 0


def cmd_gantt(args):
    xer = _load(args.file)
    s = Scheduler(xer, _pick(xer, args.project))
    html = render_html(s, title=f"openP6 - {s.project.get('proj_short_name','')}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(html)
        print(f"wrote {args.out}  ({len(s.acts)} activities)")
    else:
        sys.stdout.write(html)
    return 0


def cmd_csv(args):
    xer = _load(args.file)
    s = Scheduler(xer, _pick(xer, args.project))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["code", "name", "start", "finish", "late_start",
                "late_finish", "total_float_h", "critical", "milestone"])
    for a in s.activities():
        w.writerow([a.code, a.name, a.es_dt().strftime(FMT),
                    a.ef_dt().strftime(FMT), a.ls_dt().strftime(FMT),
                    a.lf_dt().strftime(FMT), f"{a.tf * s.hpd:.0f}",
                    int(a.critical), int(a.is_milestone)])
    text = buf.getvalue()
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="openp6", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("projects", cmd_projects), ("report", cmd_report),
                     ("gantt", cmd_gantt), ("csv", cmd_csv)):
        p = sub.add_parser(name)
        p.add_argument("file")
        p.add_argument("--project", "-p", default=None,
                       help="project id or short name (default: first)")
        if name in ("gantt", "csv"):
            p.add_argument("--out", "-o", default=None)
        p.set_defaults(func=fn)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
