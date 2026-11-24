"""Google sign-in, signed sessions, and approval tokens.

Deliberately dependency-free: urllib for the OAuth calls, HMAC for tokens.
Nothing here touches the document store, so it stays easy to test.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
SCOPE = "openid email profile"
USER_NS = "_user"                 # reserved store namespace for the user registry


def _secret() -> bytes:
    return os.environ.get("OPENP6_SECRET", "dev-secret").encode()


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(payload: dict, ttl: int) -> str:
    """Sign a JSON payload with an expiry -> '<b64>.<hmac>'."""
    body = dict(payload)
    body["exp"] = int(time.time()) + ttl
    raw = b64e(json.dumps(body, separators=(",", ":")).encode())
    mac = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{mac}"


def unsign(token: str):
    """Return the payload if the signature and expiry are valid, else None."""
    try:
        raw, mac = token.split(".")
        want = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, want):
            return None
        body = json.loads(b64d(raw))
        if int(body.get("exp", 0)) < time.time():
            return None
        return body
    except Exception:
        return None


def configured() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def admin_email() -> str:
    return (os.environ.get("ADMIN_EMAIL") or "").strip().lower()


def login_url(redirect_uri: str, state: str) -> str:
    q = {
        "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(q)


def exchange(code: str, redirect_uri: str) -> dict:
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
        "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch_userinfo(access_token: str) -> dict:
    req = urllib.request.Request(USERINFO_URL,
                                 headers={"Authorization": "Bearer " + access_token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())
