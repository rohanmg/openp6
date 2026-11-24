"""openP6 web server — a multi-user, editable P6-style scheduler over HTTP.

Auth: Google sign-in with an admin approval queue; the legacy shared password
still works as an admin fallback so the owner can never be locked out.

Storage: every project lives under its owner's namespace (`<sub>/<key>`), so
ownership is enforced server-side — knowing a key is not enough to read it.

    .venv/bin/python web/server.py
    OPENP6_PASSWORD=... GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... \
    ADMIN_EMAIL=you@example.com DATABASE_URL=... .venv/bin/python web/server.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Request, HTTPException, Body
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from openp6 import Xer                       # noqa: E402
from openp6 import model as M                # noqa: E402
from openp6.engine import schedule           # noqa: E402
from openp6.schedule import parse_dt         # noqa: E402
from store import make_store                 # noqa: E402
import auth                                  # noqa: E402
import notify                                # noqa: E402

DATA = Path(os.environ.get("OPENP6_DATA") or (ROOT / "data"))
STATIC = HERE / "static"
UPLOADS = DATA / "_uploads"
DATA.mkdir(exist_ok=True)
UPLOADS.mkdir(exist_ok=True)
STORE = make_store(DATA)

PASSWORD = os.environ.get("OPENP6_PASSWORD", "p6")
SECRET = os.environ.get("OPENP6_SECRET") or hashlib.sha256(
    (PASSWORD + "openp6").encode()).hexdigest()
COOKIE = "openp6_session"
STATE_COOKIE = "openp6_oauth_state"
FMT = "%Y-%m-%d %H:%M"
SESSION_TTL = 60 * 60 * 24 * 30

MAX_PROJECTS = int(os.environ.get("OPENP6_MAX_PROJECTS") or 10)
MAX_BYTES = int(os.environ.get("OPENP6_MAX_BYTES") or 300_000_000)   # per user
HISTORY_LIMIT = 40
HISTORY_BYTES = int(os.environ.get("OPENP6_HISTORY_BYTES") or 32_000_000)

app = FastAPI(title="openP6", docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1024)


# ------------------------------------------------------------------ sessions
def user_of(request: Request) -> dict | None:
    body = auth.unsign(request.cookies.get(COOKIE, ""))
    if not body or not body.get("sub"):
        return None
    if body.get("email", "").lower() == auth.admin_email():
        body["admin"] = True
    return body


def require(request: Request) -> dict:
    u = user_of(request)
    if not u:
        raise HTTPException(status_code=401, detail="sign in required")
    if not u.get("admin") and u.get("status") != "approved":
        raise HTTPException(status_code=403, detail="access pending approval")
    return u


def require_admin(request: Request) -> dict:
    u = require(request)
    if not u.get("admin"):
        raise HTTPException(status_code=403, detail="admin only")
    return u


def set_session(resp: Response, user: dict, request: Request) -> None:
    tok = auth.sign({"sub": user["sub"], "email": user.get("email", ""),
                     "name": user.get("name", ""), "status": user.get("status", "approved"),
                     "admin": bool(user.get("admin"))}, SESSION_TTL)
    resp.set_cookie(COOKIE, tok, httponly=True, samesite="lax", max_age=SESSION_TTL,
                    secure=request.url.scheme == "https")


def redirect_uri(request: Request) -> str:
    return f"{str(request.base_url).rstrip('/')}/auth/google/callback"


# ------------------------------------------------------------------ users
def user_key(sub: str) -> str:
    return f"{auth.USER_NS}/{sub}"


def get_user(sub: str) -> dict | None:
    return STORE.get(user_key(sub))


def put_user(u: dict) -> None:
    STORE.put(user_key(u["sub"]), u)


def all_users() -> list[dict]:
    out = []
    for k in STORE.list_keys(prefix=auth.USER_NS):
        u = STORE.get(k)
        if u:
            out.append(u)
    return out


def upsert_google_user(info: dict) -> dict:
    """Create or refresh a user record from Google's userinfo response."""
    sub = str(info.get("sub"))
    email = (info.get("email") or "").lower()
    existing = get_user(sub) or {}
    is_admin = bool(email) and email == auth.admin_email()
    u = {
        "sub": sub, "email": email, "name": info.get("name") or email,
        "picture": info.get("picture", ""),
        "status": "approved" if is_admin else (existing.get("status") or "pending"),
        "admin": is_admin,
        "first_seen": existing.get("first_seen") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if existing.get("approved_at"):
        u["approved_at"] = existing["approved_at"]
    put_user(u)
    return u


# ------------------------------------------------------------------ helpers
def owner_of(u: dict) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(u["sub"])) or "anon"


def kkey(u: dict, key: str) -> str:
    return f"{owner_of(u)}/{key}"


def summary(key: str, doc: dict) -> dict:
    acts = doc.get("activities", [])
    return {
        "key": key.split("/")[-1],
        "short_name": doc.get("short_name", ""),
        "name": doc.get("name", ""),
        "start": doc.get("start", ""),
        "finish": doc.get("finish", ""),
        "rev": doc.get("rev", 1),
        "counts": {
            "activities": len(acts),
            "critical": sum(1 for a in acts if a.get("critical")),
            "milestones": sum(1 for a in acts if a.get("milestone")),
            "relationships": sum(len(a.get("depends", [])) for a in acts),
            "resources": len(doc.get("resources", [])),
        },
    }


def _hist(key: str, seed: dict | None = None) -> dict:
    h = STORE.get(key + "__hist")
    if h:
        return h
    if seed is not None:
        h = {"states": [seed], "sizes": [len(json.dumps(seed))],
             "times": [datetime.now(timezone.utc).isoformat(timespec="seconds")], "pos": 0}
        _put_hist(key, h)
        return h
    return {"states": [], "sizes": [], "times": [], "pos": -1}


def _put_hist(key: str, h: dict) -> None:
    STORE.put(key + "__hist", h)


def _trim_history(h: dict) -> None:
    """Keep undo cheap: cap by state count AND total bytes.

    A 24 MB schedule snapshotted 40 times would be ~1 GB of history, which would
    exhaust a free Postgres on its own. Oldest states go first; one always stays.
    """
    states, sizes = h.get("states", []), h.get("sizes", [])
    times = h.get("times", [])
    while len(states) > 1 and (len(states) > HISTORY_LIMIT
                               or sum(sizes) > HISTORY_BYTES):
        states.pop(0)
        if sizes:
            sizes.pop(0)
        if times:
            times.pop(0)
    if len(states) > HISTORY_LIMIT:
        states, sizes, times = states[-HISTORY_LIMIT:], sizes[-HISTORY_LIMIT:], times[-HISTORY_LIMIT:]
    h["states"], h["sizes"], h["times"] = states, sizes, times
    h["pos"] = len(states) - 1


def load_doc(full_key: str) -> dict:
    doc = STORE.get(full_key)
    if doc is None:
        raise HTTPException(404, "unknown project")
    if doc.get("rev") is None:
        doc["rev"] = 1
    _hist(full_key, seed=doc)
    return doc


def guard_rev(doc: dict, request: Request) -> None:
    sent = request.headers.get("x-openp6-rev")
    if sent and str(sent) != str(doc.get("rev", 1)):
        raise HTTPException(409, "This schedule was changed in another session. "
                                 "Reload to see the latest, then re-apply your change.")


def save_doc(full_key: str, doc: dict) -> dict:
    doc.pop("_hpd", None)
    doc.pop("_dangling", None)
    schedule(doc)
    h = _hist(full_key)
    states = h["states"][:h["pos"] + 1]
    sizes = h.get("sizes", [])[:h["pos"] + 1]
    times = h["times"][:h["pos"] + 1]
    prev_rev = int(states[-1].get("rev", 0)) if states else 0
    doc["rev"] = prev_rev + 1
    states.append(doc)
    sizes.append(len(json.dumps(doc)))
    times.append(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    h = {"states": states, "sizes": sizes, "times": times, "pos": len(states) - 1}
    _trim_history(h)
    _put_hist(full_key, h)
    STORE.put(full_key, doc)
    _DETAIL_CACHE.pop(full_key, None)
    return doc


def _show_state(full_key: str, h: dict) -> dict:
    _put_hist(full_key, h)
    doc = h["states"][h["pos"]]
    STORE.put(full_key, doc)
    _DETAIL_CACHE.pop(full_key, None)
    return doc


_DETAIL_CACHE: dict[str, tuple] = {}
SLIM_DROP = ("es", "ef", "ls", "lf")


def slim(doc: dict) -> dict:
    out = dict(doc)
    acts = []
    for a in doc.get("activities", []):
        b = {k: v for k, v in a.items() if k not in SLIM_DROP}
        for f in ("dur_d", "total_float_h"):
            if isinstance(b.get(f), float):
                b[f] = round(b[f], 2)
        acts.append(b)
    out["activities"] = acts
    return out


def cached_detail(full_key: str, doc: dict) -> Response:
    rev = doc.get("rev", 1)
    hit = _DETAIL_CACHE.get(full_key)
    if hit and hit[0] == rev:
        return Response(hit[1], media_type="application/json")
    text = json.dumps(slim(doc), separators=(",", ":"))
    _DETAIL_CACHE[full_key] = (rev, text)
    return Response(text, media_type="application/json")


VALID_TYPES = {"TT_Task", "TT_Mile", "TT_FinMile", "TT_Rsrc", "TT_LOE", "TT_WBS"}
VALID_STATUS = {"TK_NotStart", "TK_Active", "TK_Complete"}
CSTR_CODES = {"", "CS_MSO", "CS_MSOA", "CS_SNET", "CS_MFO", "CS_MEO", "CS_FNLT",
              "CS_ALAP", "CS_MANDSTART", "CS_MANDFIN"}


def validate_activity(patch: dict, doc: dict, aid=None) -> dict:
    def num(v, lo, hi, name):
        try:
            x = float(v)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{name} must be a number")
        if not (lo <= x <= hi):
            raise HTTPException(400, f"{name} must be between {lo:g} and {hi:g}")
        return x

    if patch.get("dur_h") is not None:
        patch["dur_h"] = num(patch["dur_h"], 0, 1_000_000, "duration")
    if patch.get("pct") is not None:
        patch["pct"] = num(patch["pct"], 0, 100, "% complete")
    if patch.get("lag_h") is not None:
        patch["lag_h"] = num(patch["lag_h"], -1_000_000, 1_000_000, "lag")
    if patch.get("type") and patch["type"] not in VALID_TYPES:
        raise HTTPException(400, f"unknown activity type {patch['type']}")
    if patch.get("status") and patch["status"] not in VALID_STATUS:
        raise HTTPException(400, f"unknown status {patch['status']}")
    if patch.get("cstr_type") is not None and patch["cstr_type"] not in CSTR_CODES:
        raise HTTPException(400, f"unknown constraint type {patch['cstr_type']}")
    if patch.get("cstr_date") and parse_dt(patch["cstr_date"]) is None:
        raise HTTPException(400, "constraint date must look like YYYY-MM-DD HH:MM")
    if patch.get("wbs_id") and not any(w["id"] == patch["wbs_id"] for w in doc.get("wbs", [])):
        raise HTTPException(400, "unknown WBS element")
    if patch.get("code") is not None:
        if not str(patch["code"]).strip():
            raise HTTPException(400, "activity ID cannot be empty")
        if any(str(a["id"]) != str(aid) and a.get("code") == patch["code"]
               for a in doc["activities"]):
            raise HTTPException(400, f"activity ID {patch['code']} is already used")
    return patch


def user_usage(owner: str) -> tuple[int, int]:
    keys = STORE.list_keys(prefix=owner)
    total = 0
    for k in keys:
        try:
            total += STORE.size(k)
        except Exception:
            pass
    return len(keys), total


def check_quota(owner: str, adding: int = 1) -> None:
    n, total = user_usage(owner)
    if n + adding > MAX_PROJECTS:
        raise HTTPException(429, f"schedule limit reached ({MAX_PROJECTS} per user)")
    if total > MAX_BYTES:
        raise HTTPException(429, "storage limit reached for this account")


def import_xer_models(u: dict, path: Path, filename: str):
    xer = Xer.load(str(path))
    made = []
    for p in xer.table("PROJECT"):
        check_quota(owner_of(u))
        model = M.from_xer(xer, p["proj_id"])
        key = f"{uuid.uuid4().hex[:8]}-{p['proj_id']}"
        full = kkey(u, key)
        save_doc(full, model)
        made.append((key, model))
    return made


def find_act(doc, aid):
    a = next((x for x in doc["activities"] if str(x["id"]) == str(aid)), None)
    if a is None:
        raise HTTPException(404, f"activity {aid} not found")
    return a


def doc_new_code(doc):
    return f"A{len(doc['activities']) * 10 + 1000}"


def claim_legacy(owner: str) -> int:
    """Move pre-multi-user documents (no namespace) into an owner's space."""
    moved = 0
    for k in list(STORE.list_keys()):
        if "/" in k or k.startswith("_"):
            continue
        doc = STORE.get(k)
        if not doc:
            continue
        STORE.put(f"{owner}/{k}", doc)
        h = STORE.get(k + "__hist")
        if h:
            STORE.put(f"{owner}/{k}__hist", h)
        STORE.delete(k)
        moved += 1
    return moved


# ------------------------------------------------------------------ public
@app.get("/healthz")
def healthz():
    return {"ok": True, "app": "openP6", "store": STORE.backend,
            "projects": len([k for k in STORE.list_keys() if not k.startswith("_")]),
            "users": len(all_users()),
            "google": auth.configured(),
            "mail": bool(os.environ.get("RESEND_API_KEY") or os.environ.get("SMTP_HOST")),
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/api/me")
def me(request: Request):
    u = user_of(request)
    if not u:
        raise HTTPException(401, "not signed in")
    return {"sub": u["sub"], "email": u.get("email"), "name": u.get("name"),
            "admin": bool(u.get("admin")), "status": u.get("status", "approved")}


@app.post("/api/login")
async def login(payload: dict, request: Request):
    """Legacy shared-password login. Always grants the admin session."""
    if secrets.compare_digest(str(payload.get("password", "")), PASSWORD):
        u = {"sub": "password", "email": auth.admin_email(), "name": "Admin (password)",
             "status": "approved", "admin": True}
        if auth.admin_email():
            claim_legacy(owner_of(u))
        resp = JSONResponse({"ok": True, "admin": True})
        set_session(resp, u, request)
        return resp
    raise HTTPException(401, "wrong password")


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


# ------------------------------------------------------------------ google
@app.get("/auth/google/login")
def google_login(request: Request):
    if not auth.configured():
        return RedirectResponse("/login?error=google_not_configured")
    state = secrets.token_urlsafe(24)
    resp = RedirectResponse(auth.login_url(redirect_uri(request), state))
    resp.set_cookie(STATE_COOKIE, auth.sign({"state": state}, 600), httponly=True,
                    samesite="lax", max_age=600, secure=request.url.scheme == "https")
    return resp


@app.get("/auth/google/callback")
def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse("/login?error=" + urllib.parse.quote(error))
    st = auth.unsign(request.cookies.get(STATE_COOKIE, ""))
    if not st or st.get("state") != state or not code:
        return RedirectResponse("/login?error=bad_state")
    try:
        tokens = auth.exchange(code, redirect_uri(request))
        info = auth.fetch_userinfo(tokens.get("access_token", ""))
    except Exception as e:
        return RedirectResponse("/login?error=" + urllib.parse.quote(f"google_failed:{e}"[:80]))
    if not info.get("email") or not info.get("sub"):
        return RedirectResponse("/login?error=no_email")
    if str(info.get("email_verified", "true")).lower() == "false":
        return RedirectResponse("/login?error=email_unverified")

    was_new = get_user(str(info["sub"])) is None
    u = upsert_google_user(info)
    if u["admin"]:
        claim_legacy(owner_of(u))

    if u["status"] != "approved":
        if was_new:
            link = f"{str(request.base_url).rstrip('/')}" + \
                   "/admin/approve?token=" + auth.sign({"sub": u["sub"], "act": "approve"},
                                                       60 * 60 * 24 * 14)
            ok, detail = notify.access_request(auth.admin_email(), u, link)
            u["notified"] = detail
            put_user(u)
        resp = RedirectResponse("/pending")
        set_session(resp, u, request)
        return resp

    resp = RedirectResponse("/")
    set_session(resp, u, request)
    return resp


@app.get("/admin/approve")
def admin_approve(request: Request, token: str = ""):
    body = auth.unsign(token)
    if not body or body.get("act") != "approve":
        return Response("That approval link is invalid or has expired.", status_code=400)
    sub = str(body.get("sub"))
    u = get_user(sub)
    if not u:
        return Response("Unknown user.", status_code=404)
    u["status"] = "approved"
    u["approved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    u["approved_by"] = "email-link"
    put_user(u)
    return Response(
        f"<h3>Approved</h3><p>{u.get('email')} can now sign in to openP6.</p>"
        f"<p><a href=\"/\">Open openP6</a></p>", media_type="text/html")


# ------------------------------------------------------------------ admin
@app.get("/api/admin/users")
def admin_users(request: Request):
    require_admin(request)
    users = sorted(all_users(), key=lambda x: (x.get("status") != "pending",
                                               x.get("email", "")))
    return [{"sub": u.get("sub"), "email": u.get("email"), "name": u.get("name"),
             "status": u.get("status", "pending"), "admin": bool(u.get("admin")),
             "first_seen": u.get("first_seen"), "approved_at": u.get("approved_at"),
             "notified": u.get("notified", "")} for u in users]


@app.post("/api/admin/users/{sub}/{action}")
def admin_user_action(request: Request, sub: str, action: str):
    require_admin(request)
    if action not in ("approve", "revoke", "delete"):
        raise HTTPException(400, "unknown action")
    u = get_user(sub)
    if not u:
        raise HTTPException(404, "unknown user")
    if action == "delete":
        STORE.delete(user_key(sub))
        return {"ok": True, "deleted": True}
    u["status"] = "approved" if action == "approve" else "revoked"
    u["approved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    u["approved_by"] = "admin-ui"
    put_user(u)
    return {"ok": True, "status": u["status"]}


@app.post("/api/admin/test-email")
def admin_test_email(request: Request):
    """Prove the deployed app can actually send mail (ops check after a deploy)."""
    require_admin(request)
    ok, detail = notify.send(
        auth.admin_email(), "openP6 test email",
        "If you're reading this, openP6 can email you about access requests.",
        "<p>If you're reading this, openP6 can email you about access requests.</p>")
    return {"sent": ok, "detail": detail, "to": auth.admin_email()}


# ------------------------------------------------------------------ projects
@app.get("/api/projects")
def projects(request: Request):
    u = require(request)
    owner = owner_of(u)
    out = []
    for k in STORE.list_keys(prefix=owner):
        doc = STORE.get(k)
        if doc:
            out.append(summary(k, doc))
    out.sort(key=lambda x: x.get("short_name", ""))
    return out


@app.get("/api/usage")
def usage(request: Request):
    u = require(request)
    n, total = user_usage(owner_of(u))
    return {"projects": n, "max_projects": MAX_PROJECTS,
            "bytes": total, "max_bytes": MAX_BYTES}


@app.post("/api/projects/new")
def new_project(request: Request, payload: dict = Body(...)):
    u = require(request)
    check_quota(owner_of(u))
    code = (payload.get("short_name") or "NEW").upper()
    key = f"{uuid.uuid4().hex[:8]}-1"
    start = payload.get("start") or datetime.now().strftime("%Y-%m-%d 08:00")
    doc = {
        "proj_id": "1",
        "short_name": payload.get("short_name") or "NEW",
        "name": payload.get("name") or "New schedule",
        "start": start, "data_date": start,
        "calendar": {"name": "Standard 5 Day", "hpd": 8,
                     "workweek": [0, 1, 2, 3, 4], "holidays": []},
        "wbs": [{"id": "1", "code": code, "name": payload.get("name") or "Project",
                 "parent": "", "seq": 0}],
        "activities": [], "resources": [], "assignments": [],
    }
    full = kkey(u, key)
    save_doc(full, doc)
    return summary(full, doc)


@app.post("/api/projects")
async def upload(request: Request, file: UploadFile = File(...)):
    u = require(request)
    check_quota(owner_of(u))
    uid = uuid.uuid4().hex[:8]
    dest = UPLOADS / f"{uid}.xer"
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    try:
        made = import_xer_models(u, dest, file.filename or "upload.xer")
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"not a valid XER: {e}")
    if not made:
        raise HTTPException(400, "no PROJECT table found in that XER")
    return {"uploaded": file.filename,
            "projects": [summary(kkey(u, k), d) for k, d in made]}


@app.get("/api/projects/{key}")
def project_detail(key: str, request: Request):
    u = require(request)
    full = kkey(u, key)
    return cached_detail(full, load_doc(full))


@app.patch("/api/projects/{key}")
def update_project(key: str, request: Request, patch: dict = Body(...)):
    u = require(request)
    full = kkey(u, key)
    doc = load_doc(full)
    guard_rev(doc, request)
    for f in ("short_name", "name", "start", "data_date"):
        if f in patch and patch[f] is not None:
            if f in ("start", "data_date") and patch[f] and parse_dt(patch[f]) is None:
                raise HTTPException(400, f"{f} must look like YYYY-MM-DD HH:MM")
            doc[f] = patch[f]
    if patch.get("calendar"):
        doc["calendar"].update(patch["calendar"])
    return summary(full, save_doc(full, doc))


@app.post("/api/projects/{key}/schedule")
def reschedule(key: str, request: Request):
    u = require(request)
    full = kkey(u, key)
    doc = load_doc(full)
    guard_rev(doc, request)
    doc = save_doc(full, doc)
    doc.pop("_dangling", None)
    return doc


@app.get("/api/projects/{key}/history")
def history(key: str, request: Request):
    u = require(request)
    full = kkey(u, key)
    doc = load_doc(full)
    h = _hist(full)
    return {"rev": doc.get("rev", 1), "pos": h["pos"], "count": len(h["states"]),
            "states": [{"rev": s.get("rev"),
                        "ts": h["times"][i] if i < len(h["times"]) else "",
                        "activities": len(s.get("activities", [])),
                        "name": s.get("name", "")}
                       for i, s in enumerate(h["states"])]}


@app.post("/api/projects/{key}/undo")
def undo(key: str, request: Request):
    u = require(request)
    full = kkey(u, key)
    doc = load_doc(full)
    guard_rev(doc, request)
    h = _hist(full)
    if h["pos"] <= 0:
        raise HTTPException(400, "nothing to undo")
    h["pos"] -= 1
    d = _show_state(full, h)
    d.pop("_dangling", None)
    return d


@app.post("/api/projects/{key}/redo")
def redo(key: str, request: Request):
    u = require(request)
    full = kkey(u, key)
    doc = load_doc(full)
    guard_rev(doc, request)
    h = _hist(full)
    if h["pos"] >= len(h["states"]) - 1:
        raise HTTPException(400, "nothing to redo")
    h["pos"] += 1
    d = _show_state(full, h)
    d.pop("_dangling", None)
    return d


@app.post("/api/projects/{key}/restore")
def restore(key: str, request: Request, payload: dict = Body(...)):
    u = require(request)
    full = kkey(u, key)
    doc = load_doc(full)
    guard_rev(doc, request)
    h = _hist(full)
    rev = int(payload.get("rev", -1))
    idx = next((i for i, s in enumerate(h["states"])
                if int(s.get("rev", -1)) == rev), None)
    if idx is None:
        raise HTTPException(404, f"revision {rev} is not in this schedule's history")
    h["pos"] = idx
    d = _show_state(full, h)
    d.pop("_dangling", None)
    return d


@app.get("/api/backup")
def backup(request: Request):
    u = require(request)
    owner = owner_of(u)
    out = {}
    for k in STORE.list_keys(prefix=owner):
        out[k] = {"doc": STORE.get(k), "history": STORE.get(k + "__hist")}
    return JSONResponse(out)


# ---- activities ----------------------------------------------------------
@app.post("/api/projects/{key}/activities")
def add_activity(key: str, request: Request, payload: dict = Body(...)):
    u = require(request)
    full = kkey(u, key)
    doc = load_doc(full)
    guard_rev(doc, request)
    aid = str(payload.get("id") or M.new_activity_id(doc))
    if any(str(a["id"]) == aid for a in doc["activities"]):
        raise HTTPException(400, f"activity id {aid} already exists")
    validate_activity(payload, doc, aid)
    doc["activities"].append({
        "id": aid,
        "code": payload.get("code") or doc_new_code(doc),
        "name": payload.get("name") or "New activity",
        "wbs_id": payload.get("wbs_id") or (doc["wbs"][0]["id"] if doc.get("wbs") else ""),
        "type": payload.get("type") or "TT_Task",
        "status": payload.get("status") or "TK_NotStart",
        "dur_h": float(payload.get("dur_h") or 0),
        "pct": float(payload.get("pct") or 0),
        "cstr_type": payload.get("cstr_type") or "",
        "cstr_date": payload.get("cstr_date") or "",
        "depends": payload.get("depends") or [],
    })
    save_doc(full, doc)
    return find_act(doc, aid)


@app.patch("/api/projects/{key}/activities/{aid}")
def update_activity(key: str, request: Request, aid: str, patch: dict = Body(...)):
    u = require(request)
    full = kkey(u, key)
    doc = load_doc(full)
    a = find_act(doc, aid)
    guard_rev(doc, request)
    validate_activity(patch, doc, aid)
    for f in ("code", "name", "wbs_id", "type", "status", "cstr_type", "cstr_date"):
        if f in patch and patch[f] is not None:
            a[f] = patch[f]
    for f in ("dur_h", "pct"):
        if f in patch and patch[f] is not None:
            try:
                a[f] = float(patch[f])
            except (TypeError, ValueError):
                raise HTTPException(400, f"{f} must be a number")
    save_doc(full, doc)
    return find_act(doc, aid)


@app.delete("/api/projects/{key}/activities/{aid}")
def delete_activity(key: str, request: Request, aid: str):
    u = require(request)
    full = kkey(u, key)
    doc = load_doc(full)
    guard_rev(doc, request)
    find_act(doc, aid)
    doc["activities"] = [a for a in doc["activities"] if str(a["id"]) != str(aid)]
    for a in doc["activities"]:
        a["depends"] = [d for d in a.get("depends", []) if str(d["id"]) != str(aid)]
    save_doc(full, doc)
    return {"ok": True}


@app.post("/api/projects/{key}/activities/{aid}/depends")
def add_dep(key: str, request: Request, aid: str, payload: dict = Body(...)):
    u = require(request)
    full = kkey(u, key)
    doc = load_doc(full)
    a = find_act(doc, aid)
    guard_rev(doc, request)
    pid = str(payload.get("id", ""))
    find_act(doc, pid)
    if pid == str(aid):
        raise HTTPException(400, "an activity cannot depend on itself")
    ty = payload.get("type") or "FS"
    if ty not in ("FS", "SS", "FF", "SF"):
        raise HTTPException(400, "type must be FS, SS, FF or SF")
    adj = {}
    for x in doc["activities"]:
        for d in x.get("depends", []):
            adj.setdefault(str(d["id"]), []).append(str(x["id"]))
    seen, stack = set(), [str(aid)]
    while stack:
        n = stack.pop()
        if n == pid:
            raise HTTPException(400, "that would create a circular relationship")
        if n in seen:
            continue
        seen.add(n)
        stack.extend(adj.get(n, []))
    a.setdefault("depends", []).append(
        {"id": pid, "type": ty, "lag_h": validate_activity(
            {"lag_h": payload.get("lag_h") or 0}, doc)["lag_h"]})
    save_doc(full, doc)
    return find_act(doc, aid)


@app.delete("/api/projects/{key}/activities/{aid}/depends/{pid}")
def del_dep(key: str, request: Request, aid: str, pid: str):
    u = require(request)
    full = kkey(u, key)
    doc = load_doc(full)
    guard_rev(doc, request)
    a = find_act(doc, aid)
    a["depends"] = [d for d in a.get("depends", []) if str(d["id"]) != str(pid)]
    save_doc(full, doc)
    return find_act(doc, aid)


@app.post("/api/projects/{key}/wbs")
def add_wbs(key: str, request: Request, payload: dict = Body(...)):
    u = require(request)
    full = kkey(u, key)
    doc = load_doc(full)
    guard_rev(doc, request)
    wid = str(payload.get("id") or (len(doc["wbs"]) + 1))
    doc["wbs"].append({"id": wid, "code": payload.get("code") or wid,
                       "name": payload.get("name") or "New WBS",
                       "parent": payload.get("parent") or "",
                       "seq": float(payload.get("seq") or len(doc["wbs"]))})
    save_doc(full, doc)
    return {"id": wid}


@app.delete("/api/projects/{key}")
def delete_project(key: str, request: Request):
    u = require(request)
    full = kkey(u, key)
    load_doc(full)
    STORE.delete(full)
    _DETAIL_CACHE.pop(full, None)
    return {"ok": True}


@app.get("/api/projects/{key}/export/xer")
def export_xer(key: str, request: Request):
    u = require(request)
    full = kkey(u, key)
    doc = load_doc(full)
    xer = M.to_xer(doc)
    out = UPLOADS / f"export-{uuid.uuid4().hex[:8]}.xer"
    xer.write(str(out))
    name = (doc.get("short_name") or "schedule") + ".xer"
    return FileResponse(str(out), filename=name, media_type="text/plain")


@app.get("/api/projects/{key}/export/csv")
def export_csv(key: str, request: Request):
    u = require(request)
    doc = load_doc(kkey(u, key))
    lines = ["Activity ID,Name,WBS,Duration (d),Start,Finish,Total Float (h),Critical"]
    for a in doc["activities"]:
        wbs = next((w for w in doc["wbs"] if w["id"] == a.get("wbs_id")), {})
        lines.append(",".join(f'"{v}"' for v in [
            a.get("code", ""), a.get("name", ""), wbs.get("code", ""),
            a.get("dur_d", 0), a.get("start", ""), a.get("finish", ""),
            a.get("total_float_h", 0), "Y" if a.get("critical") else "N"]))
    name = (doc.get("short_name") or "schedule") + ".csv"
    return Response("\r\n".join(lines), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---- pages --------------------------------------------------------------
@app.get("/")
def root(request: Request):
    u = user_of(request)
    if not u:
        return RedirectResponse("/login")
    if not u.get("admin") and u.get("status") != "approved":
        return RedirectResponse("/pending")
    return FileResponse(STATIC / "index.html")


@app.get("/login")
def login_page(request: Request):
    if user_of(request):
        return RedirectResponse("/")
    return FileResponse(STATIC / "login.html")


@app.get("/pending")
def pending_page(request: Request):
    if not user_of(request):
        return RedirectResponse("/login")
    return FileResponse(STATIC / "pending.html")


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def seed():
    if STORE.list_keys():
        return
    src = ROOT / "sample.xer"
    if src.exists():
        admin = {"sub": "password"}
        import_xer_models(admin, src, "sample.xer")


if __name__ == "__main__":
    seed()
    import uvicorn
    port = int(os.environ.get("PORT") or os.environ.get("OPENP6_PORT") or "8080")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info",
                proxy_headers=True, forwarded_allow_ips="*")
