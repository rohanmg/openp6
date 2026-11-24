# Deploying openP6 — free, always-on options

openP6 ships as a Docker image with a `/healthz` endpoint and optional
Postgres storage, so it runs on any container host. Pick one path:

| Path | Always-on? | Persistent data? | Cost | Setup |
|---|---|---|---|---|
| A. Render (free) + UptimeRobot | Yes, via keep-alive ping | Via external Postgres (Neon) | $0 | 5 min |
| B. Northflank (free) | **Yes, no sleeping** | **Yes, volumes included** | $0 | 10 min |
| C. Oracle Cloud Always Free VM | **Yes** | **Yes** (200 GB disk) | $0 | 20 min |
| D. Local + Cloudflare tunnel | While your machine is on | Local disk | $0 | done |

---

## Environment variables

| Var | Meaning |
|---|---|
| `OPENP6_PASSWORD` | Admin **fallback** password (set a strong one) |
| `OPENP6_SECRET` | Stable signing secret for sessions + approval links |
| `DATABASE_URL` | Postgres DSN (Neon in production) — makes the app durable |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google OAuth client, type **Web application** |
| `ADMIN_EMAIL` | Your address: auto-approved, receives access requests |
| `RESEND_API_KEY` *or* `SMTP_HOST`/`SMTP_USER`/`SMTP_PASS` | Sends the approval email |
| `OPENP6_MAX_PROJECTS` / `OPENP6_MAX_BYTES` | Per-user quota (10 schedules / 300 MB) |
| `OPENP6_HISTORY_BYTES` | Undo-history budget per project (32 MB) |
| `OPENP6_PG_POOL` | Postgres pool size (4) |
| `OPENP6_DATA` | Local JSON directory when no `DATABASE_URL` is set |
| `PORT` / `OPENP6_PORT` | Listen port (Render injects `PORT`) |

---

## A. Render (free tier)

1. Push this repo to GitHub (see bottom).
2. Render ▸ **New ▸ Blueprint** ▸ pick the repo → it reads `render.yaml`.
3. Set `OPENP6_PASSWORD` (and `DATABASE_URL`, below) in the dashboard.
4. Deploy. Your URL is `https://openp6-xxxx.onrender.com`.

**Free-tier facts (2026):** 512 MB, sleeps after **15 min idle** (~1 min cold start),
**no persistent disk**, 750 instance-hours per workspace per month.

**Keep it awake:** UptimeRobot ▸ New Monitor ▸ HTTP(s) ▸
`https://your-app.onrender.com/healthz` ▸ every 5 minutes. One always-awake
service consumes ≈744 of the 750 free hours — it fits, but only for **one** service.

**Persistence (important!):** Render's free filesystem is wiped on every restart.
Create a free Postgres at [neon.tech](https://neon.tech) (no card) and set
`DATABASE_URL` to its connection string. openP6 stores projects as JSON docs in
Postgres, so schedules survive redeploys. Without it, treat the app as ephemeral.

> Render's free Postgres expires after 30 days — use Neon or Supabase instead.
> openP6's production instance runs on **Neon** (`ap-southeast-2`), and reuses a
> small pool of connections (`OPENP6_PG_POOL`) rather than reconnecting per call.

**Google sign-in:** create an OAuth client of type **Web application** and add the
redirect URI *exactly*:

```
https://your-app.onrender.com/auth/google/callback
```

plus `http://localhost:8080/auth/google/callback` for local testing. A mismatch
fails before Google shows any consent screen (`redirect_uri_mismatch`), so a
consent page appearing is itself proof that the URI is registered.

> **Changing an env var does not reliably restart the running instance on
> Render.** After editing env vars, trigger a deploy and confirm *that* deploy
> reached `live` — otherwise the old container keeps serving and you will end up
> "verifying" the previous database. Tell-tale sign: the API reports the new
> value while the app still behaves with the old one.

---

## B. Northflank (free, always-on, volumes)

Northflank's free tier is **$20 of resources/month, always on, no sleeping**,
with **volumes included** — the best match for "always live and free".

1. New Project ▸ New Service ▸ **Build from repo** (or Docker image).
2. Port `8080` (HTTP). Add a **Volume** mounted at `/app/data`.
3. Env: `OPENP6_PASSWORD`, `OPENP6_SECRET`.
4. The volume at `/app/data` keeps `*.json` project files across restarts —
   no external DB needed.

---

## C. Oracle Cloud Always Free VM

Always-free ARM VM (2 OCPU / 12 GB, 200 GB disk, 10 TB egress) — always on,
full disk persistence. Signup has a strict fraud filter and ARM capacity is
often scarce.

```bash
git clone <your-repo> openp6 && cd openp6
docker build -t openp6 .
docker run -d --restart=always -p 80:8080 \
  -e OPENP6_PASSWORD='your-strong-password' \
  -v /opt/openp6/data:/app/data --name openp6 openp6
```

---

## D. Local + Cloudflare quick tunnel (no account)

```bash
cd ~/openp6
OPENP6_PASSWORD='…' .venv/bin/python web/server.py &     # binds 0.0.0.0:8080
cloudflared tunnel --url http://127.0.0.1:8080            # prints an https URL
```

No account needed and it works immediately, but Cloudflare states these
account-less tunnels have **no uptime guarantee** (expect occasional 530s), and
the URL is random and dies with the process. Good for demos, not production.

---

## Push to GitHub

```bash
cd ~/openp6
git init && git add -A
git -c user.email=you@example.com -c user.name=You commit -m "openP6: P6-style scheduler"
gh repo create openp6 --public --source=. --push     # or add a remote and push
```

`.dockerignore` excludes `data/`, `out/`, `.venv/` and `.password`, so no secrets
or local schedules are published.
