"""Render a scheduled project as a self-contained HTML Gantt chart."""
from __future__ import annotations

from datetime import timedelta

FMT = "%Y-%m-%d %H:%M"


def _d(dt):
    return dt.date()


def render_html(sched, title="openP6 schedule"):
    acts = sched.activities()
    if not acts:
        return "<!doctype html><p>No activities.</p>"

    starts = [a.es_dt() for a in acts]
    finishes = [a.ef_dt() for a in acts]
    # include late dates so float is visible
    for a in acts:
        finishes.append(a.lf_dt())
    origin = min(starts).date()
    end = max(finishes).date()
    span = max((end - origin).days, 1)
    span += 1

    wbs_by_id = {w["wbs_id"]: w for w in sched.wbs()}

    def pct(day_offset):
        return 100.0 * day_offset / span

    # weekly gridlines
    ticks = []
    d = origin
    while d <= end:
        ticks.append(d)
        d += timedelta(days=7)

    rows = []
    # WBS band rows (in P6 order) with their activities
    def wbs_key(a):
        w = wbs_by_id.get(a.wbs_id)
        seq = float(w.get("seq_num") or 0) if w else 0
        return (seq, a.code)

    for a in sorted(acts, key=wbs_key):
        st, fn = a.es_dt(), a.ef_dt()
        left = pct((_d(st) - origin).days)
        width = max(pct((_d(fn) - _d(st)).days), 0.0)
        fl_left = left + width
        fl_w = max(pct((_d(a.lf_dt()) - _d(fn)).days), 0.0)
        cls = "crit" if a.critical else "norm"
        tip = (f"{a.code} {a.name}\n"
               f"Start {st.strftime(FMT)}\nFinish {fn.strftime(FMT)}\n"
               f"Total float {a.tf * sched.hpd:.0f}h ({a.tf:.1f}d)")
        if a.is_milestone:
            bar = (f'<div class="ms {cls}" style="left:{left:.3f}%" '
                   f'title="{tip}"></div>')
        else:
            bar = (f'<div class="bar {cls}" style="left:{left:.3f}%;'
                   f'width:{width:.3f}%" title="{tip}">'
                   f'<span class="lab">{a.code}</span></div>')
            if fl_w > 0:
                bar += (f'<div class="float" style="left:{fl_left:.3f}%;'
                        f'width:{fl_w:.3f}%" title="{tip}"></div>')
        rows.append(
            f'<div class="row">'
            f'<div class="label"><span class="code">{a.code}</span>'
            f'<span class="nm">{a.name}</span></div>'
            f'<div class="track">{bar}</div></div>'
        )

    axis = "".join(
        f'<div class="tick" style="left:{pct((t - origin).days):.3f}%">'
        f'<span>{t.strftime("%d %b")}</span></div>' for t in ticks
    )

    crit = sum(1 for a in acts if a.critical)
    finish_dt = max(finishes) if finishes else None

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ --fg: var(--foreground, #1c2024); --mut: var(--muted-foreground, #5b6472);
           --bd: var(--border, rgba(20,24,40,.16));
           --card: var(--card, rgba(20,24,40,.035)); }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font: 13px/1.4 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
         color: var(--fg); }}
  .summary {{ display:flex; gap:18px; flex-wrap:wrap; margin: 2px 0 12px; }}
  .kpi {{ font-size: 12px; color: var(--mut); }}
  .kpi b {{ display:block; font-size: 15px; color: var(--fg); font-weight:600; }}
  .legend {{ display:flex; gap:14px; font-size:12px; color:var(--mut); margin-bottom:8px; }}
  .sw {{ display:inline-block; width:11px; height:11px; border-radius:3px; margin-right:5px;
        vertical-align:-1px; }}
  .sw.norm {{ background:#3b82f6; }} .sw.crit {{ background:#e5484d; }}
  .sw.ms {{ background:#e5484d; transform:rotate(45deg); border-radius:1px; }}
  .chart {{ border:1px solid var(--bd); border-radius:10px; overflow:hidden; }}
  .axis {{ position:relative; height:22px; border-bottom:1px solid var(--bd);
          background:var(--card); }}
  .tick {{ position:absolute; top:0; bottom:0; border-left:1px solid var(--bd); }}
  .tick span {{ position:absolute; left:4px; top:3px; font-size:10px; color:var(--mut);
               white-space:nowrap; }}
  .row {{ display:flex; align-items:stretch; border-bottom:1px solid var(--bd); }}
  .row:last-child {{ border-bottom:none; }}
  .label {{ width:38%; min-width:180px; padding:5px 10px; display:flex; gap:8px;
           align-items:center; border-right:1px solid var(--bd); }}
  .label .code {{ color:var(--mut); font-variant-numeric:tabular-nums; min-width:52px;
                 font-size:12px; }}
  .label .nm {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .track {{ position:relative; flex:1; min-height:26px;
           background:repeating-linear-gradient(90deg, transparent 0,
             transparent calc(100% - 1px), var(--bd) calc(100% - 1px)); }}
  .bar {{ position:absolute; top:5px; height:16px; border-radius:4px; }}
  .bar.norm {{ background:#3b82f6; }} .bar.crit {{ background:#e5484d; }}
  .bar .lab {{ position:absolute; left:5px; top:0; line-height:16px; font-size:10px;
              color:#fff; }}
  .float {{ position:absolute; top:10px; height:6px; background:var(--bd); border-radius:3px; }}
  .ms {{ position:absolute; top:7px; width:12px; height:12px; margin-left:-6px;
        transform:rotate(45deg); border-radius:2px; }}
  .ms.norm {{ background:#3b82f6; }} .ms.crit {{ background:#e5484d; }}
</style></head>
<body>
  <div class="summary">
    <div class="kpi">Project<b>{sched.project.get('proj_short_name','')}</b></div>
    <div class="kpi">Start<b>{min(starts).strftime('%d %b %Y')}</b></div>
    <div class="kpi">Finish<b>{finish_dt.strftime('%d %b %Y') if finish_dt else '-'}</b></div>
    <div class="kpi">Activities<b>{len(acts)}</b></div>
    <div class="kpi">Critical<b>{crit}</b></div>
    <div class="kpi">Calendar<b>{sched.hpd:.0f}h/day</b></div>
  </div>
  <div class="legend">
    <span><i class="sw norm"></i>Non-critical</span>
    <span><i class="sw crit"></i>Critical</span>
    <span><i class="sw ms"></i>Milestone</span>
    <span><i class="sw" style="background:var(--bd)"></i>Total float</span>
  </div>
  <div class="chart">
    <div class="axis">{axis}</div>
    {''.join(rows)}
  </div>
</body></html>
"""
