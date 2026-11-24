"""Verify openP6's CPM against the dates stored in the XER."""
import sys
from openp6 import Xer
from openp6.schedule import Scheduler

FMT = "%Y-%m-%d %H:%M"


def check(path, proj_id=None):
    xer = Xer.load(path)
    print(f"\n=== {path} ===")
    print("tables:", ", ".join(xer.table_names()))
    projs = xer.table("PROJECT")
    for p in projs:
        print(f"  project {p['proj_id']:>6}  {p.get('proj_short_name',''):<10} "
              f"start={p.get('plan_start_date','')}")
    target = proj_id or projs[0]["proj_id"]
    s = Scheduler(xer, target)
    print(f"-- scheduling project {target} ({len(s.acts)} activities) --")
    if s.dangling:
        print(f"  ! dangling relationships (missing predecessor): {s.dangling}")
    ok = mism = 0
    print(f"{'code':<8}{'name':<26}{'ES(calc)':<18}{'ES(stored)':<18}"
          f"{'EF(calc)':<18}{'EF(stored)':<18}{'TFh(calc)':>9}{'TFh(store)':>11} CR")
    for a in s.activities():
        es, ef = a.es_dt(), a.ef_dt()
        se, sf = a.stored_es, a.stored_ef
        tfc = a.tf * s.hpd
        try:
            tfs = float(a.stored_tf) if a.stored_tf else None
        except ValueError:
            tfs = None
        es_m = (se is None) or (es.strftime(FMT) == se.strftime(FMT))
        ef_m = (sf is None) or (ef.strftime(FMT) == sf.strftime(FMT))
        tf_m = (tfs is None) or (abs(tfc - tfs) < 0.01)
        if es_m and ef_m and tf_m:
            ok += 1
        else:
            mism += 1
        flag = "" if (es_m and ef_m and tf_m) else "  <-- MISMATCH"
        print(f"{a.code:<8}{a.name[:25]:<26}{es.strftime(FMT):<18}"
              f"{(se.strftime(FMT) if se else '-'):<18}{ef.strftime(FMT):<18}"
              f"{(sf.strftime(FMT) if sf else '-'):<18}{tfc:>9.1f}"
              f"{(tfs if tfs is not None else -1):>11.1f} "
              f"{'Y' if a.critical else ''}{flag}")
    print(f"-- {ok} match, {mism} mismatch (out of {len(s.acts)}) --")
    return mism


if __name__ == "__main__":
    bad = 0
    for f in (sys.argv[1:] or ["sample.xer", "sample_multi.xer"]):
        try:
            bad += check(f)
        except Exception as e:
            print(f"{f}: ERROR {type(e).__name__}: {e}")
            bad += 1
    print("\nRESULT:", "ALL MATCH" if bad == 0 else f"{bad} mismatches/errors")
