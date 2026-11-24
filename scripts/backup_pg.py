#!/usr/bin/env python3
"""Back up and restore the openP6 Postgres store.

    python scripts/backup_pg.py backup  [--dsn DSN] [--out DIR]
    python scripts/backup_pg.py restore FILE [--dsn DSN]

Uses DATABASE_URL when --dsn is not given. A backup is a single JSON file holding
every row (projects, their undo history, and the user registry), so it is both a
disaster-recovery copy and a readable record. Restore is idempotent: it upserts,
so running it over a database that already has the rows is a no-op.

The connection is what makes this useful: the app's own database may be
reachable only from inside a host's network, in which case run this with the
DSN of whichever database *is* reachable.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone


def connect(dsn, prepare_threshold=None):
    import psycopg
    return psycopg.connect(dsn, autocommit=True, prepare_threshold=prepare_threshold,
                           connect_timeout=30)


def ensure_schema(c):
    c.execute("create table if not exists openp6_docs ("
              "key text primary key, doc jsonb not null, "
              "updated timestamptz default now())")


def do_backup(dsn, outdir):
    c = connect(dsn)
    ensure_schema(c)
    cur = c.execute("select key, doc, updated from openp6_docs order by key")
    rows = cur.fetchall()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, f"openp6-backup-{stamp}.json")
    payload = {
        "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_host": os.environ.get("OPENP6_BACKUP_HOST_NOTE", ""),
        "count": len(rows),
        "rows": [{"key": k, "doc": d,
                  "updated": u.isoformat() if u else None} for k, d, u in rows],
    }
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=1)
    size = os.path.getsize(path)
    print(f"backed up {len(rows)} rows -> {path} ({size:,} bytes)")
    for k, d, _ in rows:
        name = (d or {}).get("short_name")
        label = f"{len(d.get('activities', []))} activities" if d and "activities" in d \
            else ("user " + str((d or {}).get("email", "?")) if d and "email" in d else "-")
        print(f"  {k:38s} {str(name):10s} {label}")
    return path


def do_restore(dsn, path):
    with open(path) as fh:
        payload = json.load(fh)
    rows = payload.get("rows") or []
    c = connect(dsn)
    ensure_schema(c)
    for r in rows:
        c.execute("insert into openp6_docs(key,doc) values(%s,%s) "
                  "on conflict(key) do update set doc=excluded.doc, updated=now()",
                  (r["key"], json.dumps(r["doc"])))
    cur = c.execute("select count(*) from openp6_docs")
    total = cur.fetchone()[0]
    print(f"restored {len(rows)} rows from {path}; database now holds {total} rows")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["backup", "restore"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--out", default="backups")
    a = ap.parse_args()
    if not a.dsn:
        sys.exit("no DSN: pass --dsn or set DATABASE_URL")

    host = a.dsn.split("@")[-1].split("/")[0] if "@" in a.dsn else "(dsn)"
    print(f"target: {host}")
    if a.action == "backup":
        do_backup(a.dsn, a.out)
    else:
        if not a.file:
            sys.exit("restore needs a backup file")
        do_restore(a.dsn, a.file)


if __name__ == "__main__":
    main()
