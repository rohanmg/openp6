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
import os
import secrets
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Request, HTTPException, Body
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from openp6 import Xer                       # noqa: E402
from openp6 import model as M                # noqa: E402
from openp6.engine import schedule           # noqa: E402
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
    return doc


def save_doc(key: str, doc: dict) -> dict:
    doc.pop("_hpd", None)
    schedule(doc)
    STORE.put(key, doc)
    return doc


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
            "time": datetime.utcnow().isoformat() + "Z"}


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
    doc = load_doc(key)
    doc.pop("_dangling", None)
    return doc


@app.patch("/api/projects/{key}")
def update_project(key: str, request: Request, patch: dict = Body(...)):
    require(request)
    doc = load_doc(key)
    for f in ("short_name", "name", "start", "data_date"):
        if f in patch and patch[f] is not None:
            doc[f] = patch[f]
    if patch.get("calendar"):
        doc["calendar"].update(patch["calendar"])
    return {"key": key, **summary(key, save_doc(key, doc))}


@app.post("/api/projects/{key}/schedule")
def reschedule(key: str, request: Request):
    require(request)
    doc = save_doc(key, load_doc(key))
    doc.pop("_dangling", None)
    return doc


# ---- activities ----------------------------------------------------------
@app.post("/api/projects/{key}/activities")
def add_activity(key: str, request: Request, payload: dict = Body(...)):
    require(request)
    doc = load_doc(key)
    aid = str(payload.get("id") or M.new_activity_id(doc))
    if any(str(a["id"]) == aid for a in doc["activities"]):
        raise HTTPException(400, f"activity id {aid} already exists")
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
        {"id": pid, "type": ty, "lag_h": float(payload.get("lag_h") or 0)})
    save_doc(key, doc)
    return find_act(doc, aid)


@app.delete("/api/projects/{key}/activities/{aid}/depends/{pid}")
def del_dep(key: str, request: Request, aid: str, pid: str):
    require(request)
    doc = load_doc(key)
    a = find_act(doc, aid)
    a["depends"] = [d for d in a.get("depends", []) if str(d["id"]) != str(pid)]
    save_doc(key, doc)
    return find_act(doc, aid)


# ---- wbs / projects ------------------------------------------------------
@app.post("/api/projects/{key}/wbs")
def add_wbs(key: str, request: Request, payload: dict = Body(...)):
    require(request)
    doc = load_doc(key)
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
