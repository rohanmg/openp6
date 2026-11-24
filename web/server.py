"""openP6 web server — an editable P6-style scheduler over HTTP.

    .venv/bin/python web/server.py            # http://0.0.0.0:8080
    OPENP6_PASSWORD=secret DATABASE_URL=... .venv/bin/python web/server.py

Routes of note:
    GET  /healthz                        unauthenticated liveness (Render / UptimeRobot)
    POST /api/projects                   import an .xer
    POST /api/projects/new               create an empty schedule
    GET  /api/projects/{key}/export/xer  download a P6 .xer (write-back)
    PATCH/POST/DELETE .../activities...  edit activities and relationships
    POST /api/projects/{key}/schedule    recompute the CPM
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import sys
import time
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

DATA = ROOT / "data"
STATIC = HERE / "static"
DATA.mkdir(exist_ok=True)
STORE = make_store(DATA)

PASSWORD = os.environ.get("OPENP6_PASSWORD", "p6")
SECRET = os.environ.get("OPENP6_SECRET") or hashlib.sha256(
    (PASSWORD + "openp6").encode()).hexdigest()
COOKIE = "openp6_session"
FMT = "%Y-%m-%d %H:%M"

app = FastAPI(title="openP6", docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1024)   # big schedules compress ~10x


# ------------------------------------------------------------------ auth
def _token_ok(tok: str) -> bool:
    try:
        exp, sig = tok.split(".")
        want = hmac.new(SECRET.encode(), exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, want) and int(exp) > time.time()
    except Exception:
        return False


def authed(request: Request) -> bool:
    if (os.environ.get("OPENP6_LOCAL_TRUST") == "1" and request.client
            and request.client.host in ("127.0.0.1", "::1")):
        return True
    return _token_ok(request.cookies.get(COOKIE, ""))


def require(request: Request):
    if not authed(request):
        raise HTTPException(status_code=401, detail="auth required")


# ------------------------------------------------------------------ helpers
def make_key(uid: str, proj_id: str) -> str:
    return f"{uid}-{proj_id}"


def summary(key: str, doc: dict) -> dict:
    acts = doc.get("activities", [])
    return {
        "key": key,
        "short_name": doc.get("short_name", ""),
        "name": doc.get("name", ""),
        "start": doc.get("start", ""),
        "finish": doc.get("finish", ""),
        "counts": {
            "activities": len(acts),
            "critical": sum(1 for a in acts if a.get("critical")),
            "milestones": sum(1 for a in acts if a.get("milestone")),
            "relationships": sum(len(a.get("depends", [])) for a in acts),
            "resources": len(doc.get("resources", [])),
        },
    }


def load_doc(key: str) -> dict:
    doc = STORE.get(key)
    if doc is None:
        raise HTTPException(404, "unknown project")
    if doc.get("rev") is None:                 # documents written before revisions
        doc["rev"] = 1
    _hist(key, seed=doc)                       # ensure an undo history exists
    return doc


# Serialised detail responses, keyed by (key, rev). Large schedules cost ~4s to
# read + serialise + gzip, and a browser reload repeats that work unchanged.
_DETAIL_CACHE: dict[str, tuple] = {}
SLIM_DROP = ("es", "ef", "ls", "lf")           # engine internals the UI never shows


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


def cached_detail(key: str, doc: dict) -> Response:
    rev = doc.get("rev", 1)
    hit = _DETAIL_CACHE.get(key)
    if hit and hit[0] == rev:
        return Response(hit[1], media_type="application/json")
    text = json.dumps(slim(doc), separators=(",", ":"))
    _DETAIL_CACHE[key] = (rev, text)
    return Response(text, media_type="application/json")


HISTORY_LIMIT = 40
VALID_TYPES = {"TT_Task", "TT_Mile", "TT_FinMile", "TT_Rsrc", "TT_LOE", "TT_WBS"}
VALID_STATUS = {"TK_NotStart", "TK_Active", "TK_Complete"}
CSTR_CODES = {"", "CS_MSO", "CS_MSOA", "CS_SNET", "CS_MFO", "CS_MEO", "CS_FNLT",
              "CS_ALAP", "CS_MANDSTART", "CS_MANDFIN"}


def _hist(key: str, seed: dict | None = None) -> dict:
    h = STORE.get(key + "__hist")
    if h:
        return h
    if seed is not None:                       # migrate a pre-history document
        h = {"states": [seed],
             "times": [datetime.now(timezone.utc).isoformat(timespec="seconds")],
             "pos": 0}
        _put_hist(key, h)
        return h
    return {"states": [], "times": [], "pos": -1}


def _put_hist(key: str, h: dict) -> None:
    STORE.put(key + "__hist", h)


def guard_rev(doc: dict, request: Request) -> None:
    """Optimistic concurrency: reject a write built on a stale revision."""
    sent = request.headers.get("x-openp6-rev")
    if sent and str(sent) != str(doc.get("rev", 1)):
        raise HTTPException(409, "This schedule was changed in another session. "
                                 "Reload to see the latest, then re-apply your change.")


def save_doc(key: str, doc: dict) -> dict:
    """Persist a new revision, keeping a bounded undo history alongside it."""
    doc.pop("_hpd", None)
    doc.pop("_dangling", None)
    schedule(doc)
    h = _hist(key)
    states = h["states"][:h["pos"] + 1]            # drop any redo branch
    times = h["times"][:h["pos"] + 1]
    prev_rev = int(states[-1].get("rev", 0)) if states else 0
    doc["rev"] = prev_rev + 1
    states.append(doc)
    times.append(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if len(states) > HISTORY_LIMIT:
        states, times = states[-HISTORY_LIMIT:], times[-HISTORY_LIMIT:]
    _put_hist(key, {"states": states, "times": times, "pos": len(states) - 1})
    STORE.put(key, doc)
    _DETAIL_CACHE.pop(key, None)               # response cache is now stale
    return doc


def _show_state(key: str, h: dict) -> dict:
    _put_hist(key, h)
    doc = h["states"][h["pos"]]
    STORE.put(key, doc)
    return doc


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


def import_xer_models(path: Path, filename: str):
    xer = Xer.load(str(path))
    uid = uuid.uuid4().hex[:8]
    out = []
    for p in xer.table("PROJECT"):
        model = M.from_xer(xer, p["proj_id"])
        key = make_key(uid, p["proj_id"])
        save_doc(key, model)
        out.append((key, model))
    return out


def find_act(doc, aid):
    a = next((x for x in doc["activities"] if str(x["id"]) == str(aid)), None)
    if a is None:
        raise HTTPException(404, f"activity {aid} not found")
    return a


def doc_new_code(doc):
    return f"A{len(doc['activities']) * 10 + 1000}"


# ------------------------------------------------------------------ routes
@app.get("/healthz")
def healthz():
    return {"ok": True, "app": "openP6", "store": STORE.backend,
            "projects": len(STORE.list_keys()),
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.post("/api/login")
async def login(payload: dict, request: Request):
    if secrets.compare_digest(str(payload.get("password", "")), PASSWORD):
        exp = str(int(time.time()) + 60 * 60 * 24 * 30)
        sig = hmac.new(SECRET.encode(), exp.encode(), hashlib.sha256).hexdigest()
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, f"{exp}.{sig}", httponly=True, samesite="lax",
                        max_age=60 * 60 * 24 * 30, secure=request.url.scheme == "https")
        return resp
    raise HTTPException(401, "wrong password")


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/projects")
def projects(request: Request):
    require(request)
    out = []
    for k in STORE.list_keys():
        doc = STORE.get(k)
        if doc:
            out.append(summary(k, doc))
    out.sort(key=lambda x: x.get("short_name", ""))
    return out


@app.post("/api/projects/new")
def new_project(request: Request, payload: dict = Body(...)):
    require(request)
    code = (payload.get("short_name") or "NEW").upper()
    key = make_key(uuid.uuid4().hex[:8], "1")
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
    save_doc(key, doc)
    return {"key": key, **summary(key, doc)}


@app.post("/api/projects")
async def upload(request: Request, file: UploadFile = File(...)):
    require(request)
    uid = uuid.uuid4().hex[:8]
    dest = DATA / f"{uid}.xer"
    with dest.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    try:
        made = import_xer_models(dest, file.filename or "upload.xer")
    except HTTPException:
        raise
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"not a valid XER: {e}")
    if not made:
        raise HTTPException(400, "no PROJECT table found in that XER")
    return {"uploaded": file.filename,
            "projects": [{"key": k, **summary(k, d)} for k, d in made]}


@app.get("/api/projects/{key}")
def project_detail(key: str, request: Request):
    require(request)
    return cached_detail(key, load_doc(key))


@app.patch("/api/projects/{key}")
def update_project(key: str, request: Request, patch: dict = Body(...)):
    require(request)
    doc = load_doc(key)
    guard_rev(doc, request)
    for f in ("short_name", "name", "start", "data_date"):
        if f in patch and patch[f] is not None:
            if f in ("start", "data_date") and patch[f] and parse_dt(patch[f]) is None:
                raise HTTPException(400, f"{f} must look like YYYY-MM-DD HH:MM")
            doc[f] = patch[f]
    if patch.get("calendar"):
        doc["calendar"].update(patch["calendar"])
    return {"key": key, **summary(key, save_doc(key, doc))}


@app.post("/api/projects/{key}/schedule")
def reschedule(key: str, request: Request):
    require(request)
    doc = load_doc(key)
    guard_rev(doc, request)
    doc = save_doc(key, doc)
    doc.pop("_dangling", None)
    return doc


# ---- history: undo / redo / restore / backup ----------------------------
@app.get("/api/projects/{key}/history")
def history(key: str, request: Request):
    require(request)
    doc = load_doc(key)
    h = _hist(key)
    return {
        "rev": doc.get("rev", 1),
        "pos": h["pos"],
        "count": len(h["states"]),
        "states": [{"rev": s.get("rev"), "ts": h["times"][i] if i < len(h["times"]) else "",
                    "activities": len(s.get("activities", [])),
                    "name": s.get("name", "")}
                   for i, s in enumerate(h["states"])],
    }


@app.post("/api/projects/{key}/undo")
def undo(key: str, request: Request):
    require(request)
    doc = load_doc(key)
    guard_rev(doc, request)
    h = _hist(key)
    if h["pos"] <= 0:
        raise HTTPException(400, "nothing to undo")
    h["pos"] -= 1
    d = _show_state(key, h)
    d.pop("_dangling", None)
    return d


@app.post("/api/projects/{key}/redo")
def redo(key: str, request: Request):
    require(request)
    doc = load_doc(key)
    guard_rev(doc, request)
    h = _hist(key)
    if h["pos"] >= len(h["states"]) - 1:
        raise HTTPException(400, "nothing to redo")
    h["pos"] += 1
    d = _show_state(key, h)
    d.pop("_dangling", None)
    return d


@app.post("/api/projects/{key}/restore")
def restore(key: str, request: Request, payload: dict = Body(...)):
    require(request)
    doc = load_doc(key)
    guard_rev(doc, request)
    h = _hist(key)
    rev = int(payload.get("rev", -1))
    idx = next((i for i, s in enumerate(h["states"])
                if int(s.get("rev", -1)) == rev), None)
    if idx is None:
        raise HTTPException(404, f"revision {rev} is not in this schedule's history")
    h["pos"] = idx
    d = _show_state(key, h)
    d.pop("_dangling", None)
    return d


@app.get("/api/backup")
def backup(request: Request):
    require(request)
    out = {}
    for k in STORE.list_keys():
        out[k] = {"doc": STORE.get(k), "history": STORE.get(k + "__hist")}
    return JSONResponse(out)


# ---- activities ----------------------------------------------------------
@app.post("/api/projects/{key}/activities")
def add_activity(key: str, request: Request, payload: dict = Body(...)):
    require(request)
    doc = load_doc(key)
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
    save_doc(key, doc)
    return find_act(doc, aid)


@app.patch("/api/projects/{key}/activities/{aid}")
def update_activity(key: str, request: Request, aid: str, patch: dict = Body(...)):
    require(request)
    doc = load_doc(key)
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
    save_doc(key, doc)
    return find_act(doc, aid)


@app.delete("/api/projects/{key}/activities/{aid}")
def delete_activity(key: str, request: Request, aid: str):
    require(request)
    doc = load_doc(key)
    guard_rev(doc, request)
    find_act(doc, aid)
    doc["activities"] = [a for a in doc["activities"] if str(a["id"]) != str(aid)]
    for a in doc["activities"]:
        a["depends"] = [d for d in a.get("depends", []) if str(d["id"]) != str(aid)]
    save_doc(key, doc)
    return {"ok": True}


# ---- relationships -------------------------------------------------------
@app.post("/api/projects/{key}/activities/{aid}/depends")
def add_dep(key: str, request: Request, aid: str, payload: dict = Body(...)):
    require(request)
    doc = load_doc(key)
    a = find_act(doc, aid)
    guard_rev(doc, request)
    pid = str(payload.get("id", ""))
    find_act(doc, pid)
    if pid == str(aid):
        raise HTTPException(400, "an activity cannot depend on itself")
    ty = payload.get("type") or "FS"
    if ty not in ("FS", "SS", "FF", "SF"):
        raise HTTPException(400, "type must be FS, SS, FF or SF")
    # cycle guard: adding pred -> aid is illegal if aid can already reach pred
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
    save_doc(key, doc)
    return find_act(doc, aid)


@app.delete("/api/projects/{key}/activities/{aid}/depends/{pid}")
def del_dep(key: str, request: Request, aid: str, pid: str):
    require(request)
    doc = load_doc(key)
    guard_rev(doc, request)
    a = find_act(doc, aid)
    a["depends"] = [d for d in a.get("depends", []) if str(d["id"]) != str(pid)]
    save_doc(key, doc)
    return find_act(doc, aid)


# ---- wbs / projects ------------------------------------------------------
@app.post("/api/projects/{key}/wbs")
def add_wbs(key: str, request: Request, payload: dict = Body(...)):
    require(request)
    doc = load_doc(key)
    guard_rev(doc, request)
    wid = str(payload.get("id") or (len(doc["wbs"]) + 1))
    doc["wbs"].append({"id": wid, "code": payload.get("code") or wid,
                       "name": payload.get("name") or "New WBS",
                       "parent": payload.get("parent") or "",
                       "seq": float(payload.get("seq") or len(doc["wbs"]))})
    save_doc(key, doc)
    return {"id": wid}


@app.delete("/api/projects/{key}")
def delete_project(key: str, request: Request):
    require(request)
    STORE.delete(key)
    _DETAIL_CACHE.pop(key, None)
    return {"ok": True}


# ---- export --------------------------------------------------------------
@app.get("/api/projects/{key}/export/xer")
def export_xer(key: str, request: Request):
    require(request)
    doc = load_doc(key)
    xer = M.to_xer(doc)
    out = DATA / f"export-{key}.xer"
    xer.write(str(out))
    name = (doc.get("short_name") or "schedule") + ".xer"
    return FileResponse(str(out), filename=name, media_type="text/plain")


@app.get("/api/projects/{key}/export/csv")
def export_csv(key: str, request: Request):
    require(request)
    doc = load_doc(key)
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


# ---- static --------------------------------------------------------------
@app.get("/")
def root(request: Request):
    if not authed(request):
        return RedirectResponse("/login")
    return FileResponse(STATIC / "index.html")


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def seed():
    if STORE.list_keys():
        return
    src = ROOT / "sample.xer"
    if src.exists():
        import_xer_models(src, "sample.xer")


if __name__ == "__main__":
    seed()
    import uvicorn
    port = int(os.environ.get("PORT") or os.environ.get("OPENP6_PORT") or "8080")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
