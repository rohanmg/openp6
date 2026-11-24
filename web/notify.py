"""Notify the admin that someone wants access.

Tries, in order: Resend API key -> SMTP creds -> nothing. When no sender is
configured the notification is still recorded (so the admin sees the request in
the in-app queue) and the failure is logged, never raised into the request path.

Env:
    RESEND_API_KEY, MAIL_FROM
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM
    ADMIN_EMAIL
"""
from __future__ import annotations

import json
import os
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

# Cloudflare fronts api.resend.com and blocks requests carrying a default
# scripting User-Agent with "error code: 1010", so always identify ourselves.
UA = "openP6/1.0 (+https://openp6.onrender.com)"


def _from() -> str:
    return os.environ.get("MAIL_FROM") or os.environ.get("SMTP_FROM") or "openP6 <onboarding@resend.dev>"


def send(to: str, subject: str, text: str, html: str = "") -> tuple[bool, str]:
    """Return (sent, detail). Never raises."""
    if not to:
        return False, "no recipient configured"

    key = os.environ.get("RESEND_API_KEY")
    if key:
        try:
            body = json.dumps({"from": _from(), "to": [to], "subject": subject,
                               "text": text, "html": html or None}).encode()
            req = urllib.request.Request(
                "https://api.resend.com/emails", data=body, method="POST",
                headers={"Authorization": "Bearer " + key,
                         "Content-Type": "application/json",
                         "Accept": "application/json",
                         "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return 200 <= r.status < 300, f"resend {r.status}"
        except urllib.error.HTTPError as e:
            try:
                why = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                why = ""
            return False, f"resend {e.code}: {why}"
        except Exception as e:
            return False, f"resend failed: {e}"

    host = os.environ.get("SMTP_HOST")
    if host:
        try:
            msg = EmailMessage()
            msg["From"] = _from()
            msg["To"] = to
            msg["Subject"] = subject
            msg.set_content(text)
            if html:
                msg.add_alternative(html, subtype="html")
            port = int(os.environ.get("SMTP_PORT") or 587)
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls()
                user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
                if user and pw:
                    s.login(user, pw)
                s.send_message(msg)
            return True, "smtp sent"
        except Exception as e:
            return False, f"smtp failed: {e}"

    return False, "no mail sender configured (set RESEND_API_KEY or SMTP_* )"


def access_request(admin: str, user: dict, approve_url: str) -> tuple[bool, str]:
    name = user.get("name") or user.get("email") or "Someone"
    email = user.get("email", "")
    text = (
        f"{name} ({email}) has requested access to openP6.\n\n"
        f"Approve them:\n{approve_url}\n\n"
        f"Or review and approve from the app's Users panel.\n"
    )
    html = (
        f"<p><b>{name}</b> ({email}) has requested access to openP6.</p>"
        f'<p><a href="{approve_url}">Approve this request</a></p>'
        f"<p style='color:#666'>Or review it from the app's Users panel.</p>"
    )
    return send(admin, f"openP6 access request: {email}", text, html)
