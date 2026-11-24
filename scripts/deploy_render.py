#!/usr/bin/env python3
"""Deploy openP6 to Render from this repository.

Usage:
    export RENDER_API_KEY=rnd_xxx
    python3 scripts/deploy_render.py --password 'YOUR_PASSWORD' [--secret HEX] \
        [--name openp6] [--repo https://github.com/OWNER/REPO] [--database]

Creates (or reuses) a Render blueprint/service for this repo, wires the
environment variables, waits for the first deploy, and prints the public URL.

--database also provisions Render's free Postgres and links it as DATABASE_URL.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.render.com/v1"


def call(method: str, path: str, body=None, key: str = ""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="openp6")
    ap.add_argument("--repo", default="https://github.com/rohanmg/openp6")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--password", required=True)
    ap.add_argument("--secret", default=secrets.token_hex(32))
    ap.add_argument("--database", action="store_true",
                    help="also create a free Render Postgres and link DATABASE_URL")
    ap.add_argument("--region", default="oregon")
    args = ap.parse_args()

    key = os.environ.get("RENDER_API_KEY")
    if not key:
        print("RENDER_API_KEY is not set", file=sys.stderr)
        return 2

    code, owners = call("GET", "/owners", key=key)
    if code != 200 or not isinstance(owners, list) or not owners:
        print(f"cannot read owners: {code} {owners}", file=sys.stderr)
        return 1
    first = owners[0] if isinstance(owners[0], dict) else {}
    owner_id = (first.get("owner") or {}).get("id")
    if not owner_id:
        print(f"no owner id in response: {owners}", file=sys.stderr)
        return 1
    print(f"owner        : {owner_id}")

    code, existing = call("GET", f"/services?name={args.name}&limit=20", key=key)
    svc = None
    if code == 200 and isinstance(existing, list):
        for item in existing:
            s = item.get("service", item) if isinstance(item, dict) else {}
            if isinstance(s, dict) and s.get("name") == args.name:
                svc = s
                print(f"reusing service: {s.get('id')}")
                break

    # ---- database first, so the service is created with DATABASE_URL already set ----
    db_url = ""
    if args.database:
        db = None
        code, dbs = call("GET", "/postgres?limit=20", key=key)
        if code == 200 and isinstance(dbs, list):
            for it in dbs:
                p = it.get("postgres", it) if isinstance(it, dict) else {}
                if isinstance(p, dict) and p.get("name") == f"{args.name}-db":
                    db = p
                    print(f"reusing postgres: {db.get('id')}")
                    break
        if db is None:
            code, resp = call("POST", "/postgres", {
                "name": f"{args.name}-db", "ownerId": owner_id, "plan": "free",
                "region": args.region, "version": "16"}, key=key)
            if code in (200, 201):
                db = resp.get("postgres", resp) if isinstance(resp, dict) else {}
                print(f"postgres      : {db.get('id')} (free plan expires after 30 days)")
            else:
                print(f"postgres create failed: {code} {resp}", file=sys.stderr)
                db = None
        if db and db.get("id"):
            for _ in range(30):                 # a new Postgres is not ready instantly
                code, conn = call("GET", f"/postgres/{db['id']}/connection-info", key=key)
                if code == 200 and isinstance(conn, dict) and conn.get("internalConnectionString"):
                    db_url = conn["internalConnectionString"]
                    break
                time.sleep(10)
            print(f"db url       : {'resolved' if db_url else 'NOT READY (rerun to attach)'}")

    env_vars = [
        {"key": "OPENP6_PASSWORD", "value": args.password},
        {"key": "OPENP6_SECRET", "value": args.secret},
    ]
    if db_url:
        env_vars.append({"key": "DATABASE_URL", "value": db_url})

    if svc is None:
        body = {
            "type": "web_service",
            "name": args.name,
            "ownerId": owner_id,
            "repo": args.repo,
            "branch": args.branch,
            "autoDeploy": "yes",
            "envVars": env_vars,
            "serviceDetails": {
                "env": "docker",
                "plan": "free",
                "region": args.region,
                "healthCheckPath": "/healthz",
                "envSpecificDetails": {"dockerfilePath": "./Dockerfile"},
            },
        }
        code, resp = call("POST", "/services", body, key=key)
        if code not in (200, 201):
            print(f"service create failed: {code} {resp}", file=sys.stderr)
            return 1
        svc = resp.get("service", resp) if isinstance(resp, dict) else {}
        print(f"created       : {svc.get('id')}")
    elif db_url:
        # per-key endpoint: the bulk PUT rejects this payload shape
        code, resp = call("PUT", f"/services/{svc['id']}/env-vars/DATABASE_URL",
                          {"value": db_url}, key=key)
        print(f"env vars     : DATABASE_URL attach -> HTTP {code}")

    if not isinstance(svc, dict) or not svc.get("id"):
        print(f"could not determine service id: {svc}", file=sys.stderr)
        return 1

    url = (svc.get("serviceDetails") or {}).get("url") or svc.get("url") or ""
    print(f"url           : {url}")

    if url:
        subprocess.run(["gh", "variable", "set", "OPENP6_URL", "--body", url],
                       check=False)

    print("\nwaiting for the first deploy (free tier builds take a few minutes)…")
    for _ in range(60):
        time.sleep(20)
        code, deploys = call("GET", f"/services/{svc['id']}/deploys?limit=1", key=key)
        if code == 200 and isinstance(deploys, list) and deploys:
            item = deploys[0] if isinstance(deploys[0], dict) else {}
            d = item.get("deploy", item)
            status = d.get("status") if isinstance(d, dict) else None
            if status in ("live", "build_failed", "update_failed", "canceled"):
                print(f"deploy status: {status}")
                break
    if url:
        print(f"\nDone. Open {url} and sign in with your OPENP6_PASSWORD.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
