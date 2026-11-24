"""Document store for openP6 projects.

Local JSON files by default; Postgres when DATABASE_URL is set (so edits
survive redeploys on hosts whose filesystem is ephemeral, e.g. Render free).
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class LocalStore:
    backend = "local"

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key):
        return self.root / (key.replace("/", "_") + ".json")

    def list_keys(self):
        return sorted(p.stem for p in self.root.glob("*.json"))

    def get(self, key):
        p = self._p(key)
        return json.loads(p.read_text()) if p.exists() else None

    def put(self, key, doc):
        self._p(key).write_text(json.dumps(doc))

    def delete(self, key):
        self._p(key).unlink(missing_ok=True)


class PGStore:
    backend = "postgres"

    def __init__(self, dsn):
        import psycopg  # noqa
        self.psycopg = psycopg
        self.dsn = dsn
        with self._conn() as c:
            c.execute("create table if not exists openp6_docs ("
                      "key text primary key, doc jsonb not null, "
                      "updated timestamptz default now())")

    def _conn(self):
        return self.psycopg.connect(self.dsn, autocommit=True)

    def list_keys(self):
        with self._conn() as c:
            return [r[0] for r in c.execute("select key from openp6_docs order by key")]

    def get(self, key):
        with self._conn() as c:
            cur = c.execute("select doc from openp6_docs where key=%s", (key,))
            row = cur.fetchone()
            return row[0] if row else None

    def put(self, key, doc):
        with self._conn() as c:
            c.execute("insert into openp6_docs(key,doc) values(%s,%s) "
                      "on conflict(key) do update set doc=excluded.doc, updated=now()",
                      (key, json.dumps(doc)))

    def delete(self, key):
        with self._conn() as c:
            c.execute("delete from openp6_docs where key=%s", (key,))


def make_store(root):
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        try:
            return PGStore(dsn)
        except Exception as e:                       # pragma: no cover
            print("! Postgres store unavailable, using local files:", e)
    return LocalStore(root)
