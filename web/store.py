"""Document store for openP6 projects.

Local JSON files by default; Postgres when DATABASE_URL is set (so edits
survive redeploys on hosts whose filesystem is ephemeral, e.g. Render free).
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
from pathlib import Path


class LocalStore:
    backend = "local"

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key):
        """Keys may be namespaced as '<owner>/<id>' -> a per-owner subdirectory."""
        parts = [p for p in key.split("/") if p]
        if len(parts) <= 1:
            return self.root / ((parts[0] if parts else "doc") + ".json")
        d = self.root / parts[0]
        d.mkdir(parents=True, exist_ok=True)
        return d / ("_".join(parts[1:]) + ".json")

    def list_keys(self, prefix=""):
        """All document keys, optionally restricted to one namespace."""
        out = []
        if prefix:
            d = self.root / prefix
            if d.is_dir():
                out = [f"{prefix}/{p.stem}" for p in d.glob("*.json")]
        else:
            out = [p.stem for p in self.root.glob("*.json")]
            for d in sorted(self.root.iterdir()):
                if d.is_dir() and not d.name.startswith("."):
                    out += [f"{d.name}/{p.stem}" for p in d.glob("*.json")]
        return sorted(k for k in out if not k.endswith("__hist"))

    def get(self, key):
        p = self._p(key)
        return json.loads(p.read_text()) if p.exists() else None

    def put(self, key, doc):
        """Crash-safe write: temp file + fsync + atomic rename."""
        p = self._p(key)
        tmp = p.with_name(p.name + ".tmp")
        with open(tmp, "w") as fh:
            fh.write(json.dumps(doc))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)

    def delete(self, key):
        self._p(key).unlink(missing_ok=True)
        self._p(key + "__hist").unlink(missing_ok=True)

    def size(self, key):
        p = self._p(key)
        return p.stat().st_size if p.exists() else 0


class PGStore:
    """Postgres store with a small connection pool.

    A connection per call is fine on a host's internal network, but against a
    managed database over the internet every call would pay a TLS handshake -
    and one page load issues dozens of store calls.
    """
    backend = "postgres"
    POOL_MAX = int(os.environ.get("OPENP6_PG_POOL") or 4)

    def __init__(self, dsn):
        import psycopg  # noqa
        self.psycopg = psycopg
        self.dsn = dsn
        self._pool = []
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute("create table if not exists openp6_docs ("
                      "key text primary key, doc jsonb not null, "
                      "updated timestamptz default now())")

    def _new(self):
        # prepare_threshold=None: a pooled/pgBouncer endpoint like Neon's can route
        # the next statement to a different backend, so a server-side prepared
        # statement can vanish mid-connection and error on reuse.
        return self.psycopg.connect(self.dsn, autocommit=True,
                                    prepare_threshold=None, connect_timeout=30)

    def _acquire(self):
        with self._lock:
            c = self._pool.pop() if self._pool else None
        if c is not None:
            try:
                if not c.closed:
                    c.execute("select 1")      # a scale-to-zero DB drops idle handles
                    return c
            except Exception:
                pass
            try:
                c.close()
            except Exception:
                pass
        return self._new()

    def _release(self, c):
        with self._lock:
            if len(self._pool) < self.POOL_MAX:
                self._pool.append(c)
                return
        try:
            c.close()
        except Exception:
            pass

    @contextlib.contextmanager
    def _conn(self):
        c = self._acquire()
        try:
            yield c
        except Exception:
            try:
                c.close()
            except Exception:
                pass
            raise
        else:
            self._release(c)

    def list_keys(self, prefix=""):
        like = (prefix + "/%") if prefix else "%"
        with self._conn() as c:
            cur = c.execute("select key from openp6_docs "
                            "where key like %s and key not like '%%__hist' order by key",
                            (like,))
            return [r[0] for r in cur]

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
            c.execute("delete from openp6_docs where key=%s or key=%s",
                      (key, key + "__hist"))

    def size(self, key):
        with self._conn() as c:
            cur = c.execute("select coalesce(length(doc::text),0) "
                            "from openp6_docs where key=%s", (key,))
            row = cur.fetchone()
            return int(row[0]) if row else 0


def make_store(root):
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        try:
            return PGStore(dsn)
        except Exception as e:                       # pragma: no cover
            print("! Postgres store unavailable, using local files:", e)
    return LocalStore(root)
