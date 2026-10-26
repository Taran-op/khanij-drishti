"""Layer 5 · Notifications: real Email (SMTP) and SMS (Twilio-compatible REST) delivery.

  settings     SMTP relay + SMS gateway, editable by an administrator. Secrets (SMTP password, SMS
               auth token) can come from env vars KD_SMTP_PASSWORD / KD_SMS_TOKEN instead of the
               store, and are never returned by the API.
  recipients   people / distribution lists: channel, address, audiences (operations, planners,
               geology, management), minimum severity.
  outbox       every message ever queued, with status queued → sent | failed (retried up to 3×)
               | held (channel not configured / no recipient). Survives pipeline rebuilds.

Alerts are de-duplicated: the same alert (category + subject + severity) is not re-sent to the
same address within 24 h, so re-running the pipeline doesn't spam people.

Dev sinks (no real credentials needed to see delivery work end to end):
    python -m kd.notify sink            # SMTP sink on localhost:1025 → data/mail_sink/*.eml
    KD_DEV_SINKS=1 uvicorn …            # Twilio-compatible SMS sink at /dev/sms/…  → data/sms_sink.jsonl
"""
import json
import mimetypes
import os
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import requests

from . import db
from .config import DATA

DB = DATA / "notify.sqlite"
SEVERITY = {"info": 0, "medium": 1, "high": 2}
AUDIENCES = ("operations", "planners", "geology", "management")
MAX_ATTEMPTS = 3
DEFAULTS = dict(
    smtp_host="", smtp_port="587", smtp_security="starttls", smtp_user="", smtp_from="KhanijSetu <alerts@localhost>",
    sms_base_url="https://api.twilio.com", sms_account_sid="", sms_from="",
    email_enabled="0", sms_enabled="0",
)
SECRETS = {"smtp_password": "KD_SMTP_PASSWORD", "sms_auth_token": "KD_SMS_TOKEN"}


def _con():
    return db.connect("notify")


def init():
    with _con() as c:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("""CREATE TABLE IF NOT EXISTS recipients (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, channel TEXT,
                     address TEXT, audiences TEXT, min_severity TEXT DEFAULT 'high', active INTEGER DEFAULT 1, created TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, ref TEXT,
                     channel TEXT, recipient TEXT, address TEXT, subject TEXT, body TEXT, attachment TEXT, dedup_key TEXT,
                     status TEXT, attempts INTEGER DEFAULT 0, last_error TEXT, sent_at TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_outbox_status ON outbox(status)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_outbox_dedup ON outbox(dedup_key, address)")


# ---- settings --------------------------------------------------------------------------------
def _raw_settings():
    init()
    with _con() as c:
        s = dict(DEFAULTS, **{r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings")})
    for k, env in SECRETS.items():
        if os.environ.get(env):
            s[k] = os.environ[env]
    return s


def settings_public():
    s = _raw_settings()
    out = {k: v for k, v in s.items() if k not in SECRETS}
    out.update({f"{k}_set": bool(s.get(k)) for k in SECRETS})
    out.update({f"{k}_from_env": bool(os.environ.get(env)) for k, env in SECRETS.items()})
    out["email_ready"], out["sms_ready"] = _email_ready(s), _sms_ready(s)
    return out


def update_settings(values):
    allowed = set(DEFAULTS) | set(SECRETS)
    init()
    with _con() as c:
        for k, v in values.items():
            if k not in allowed or v is None:
                continue
            if k in SECRETS and v == "":
                continue  # blank secret field = keep the stored one
            c.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (k, str(v)))
    return settings_public()


def _email_ready(s):
    return s["email_enabled"] == "1" and bool(s["smtp_host"]) and bool(s["smtp_from"])


def _sms_ready(s):
    return s["sms_enabled"] == "1" and all(s.get(k) for k in ("sms_base_url", "sms_account_sid", "sms_auth_token", "sms_from"))


# ---- recipients ------------------------------------------------------------------------------
def recipients(active_only=False):
    init()
    with _con() as c:
        q = "SELECT * FROM recipients" + (" WHERE active=1" if active_only else "") + " ORDER BY name"
        return [dict(r) for r in c.execute(q)]


def add_recipient(name, channel, address, audiences, min_severity="high"):
    if channel not in ("email", "sms"):
        raise ValueError("channel must be email or sms")
    if channel == "email" and "@" not in address:
        raise ValueError("not an email address")
    if channel == "sms" and not (address.startswith("+") and address[1:].replace(" ", "").isdigit()):
        raise ValueError("SMS numbers must be in E.164 form, e.g. +919800000000")
    bad = set(audiences) - set(AUDIENCES)
    if bad or not audiences:
        raise ValueError(f"audiences must be among {AUDIENCES}")
    if min_severity not in SEVERITY:
        raise ValueError("min_severity must be info, medium or high")
    init()
    with _con() as c:
        cur = c.execute("INSERT INTO recipients (name, channel, address, audiences, min_severity, active, created) VALUES (?,?,?,?,?,1,?)",
                        (name, channel, address.replace(" ", "") if channel == "sms" else address, ",".join(audiences),
                         min_severity, datetime.now().isoformat(timespec="seconds")))
        return cur.lastrowid


def set_recipient_active(rid, active):
    with _con() as c:
        c.execute("UPDATE recipients SET active=? WHERE id=?", (int(bool(active)), rid))


def delete_recipient(rid):
    with _con() as c:
        c.execute("DELETE FROM recipients WHERE id=?", (rid,))


# ---- queueing --------------------------------------------------------------------------------
def _queue(c, kind, ref, r, subject, body, attachment=None, dedup_key=None):
    c.execute("""INSERT INTO outbox (ts, kind, ref, channel, recipient, address, subject, body, attachment, dedup_key, status)
                 VALUES (?,?,?,?,?,?,?,?,?,?, 'queued')""",
              (datetime.now().isoformat(timespec="seconds"), kind, ref, r["channel"], r["name"], r["address"], subject, body,
               str(attachment) if attachment else None, dedup_key))


def _recently_sent(c, key, address, hours=24):
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    return c.execute("SELECT 1 FROM outbox WHERE dedup_key=? AND address=? AND ts>=? AND status IN ('queued','sent')",
                     (key, address, since)).fetchone() is not None


def enqueue_alerts(alerts):
    """Fan each alert out to the active recipients whose audience and minimum severity match."""
    rs = recipients(active_only=True)
    n = skipped = 0
    with _con() as c:
        for a in alerts:
            aud = set(a["audience"].split(","))
            key = f"alert|{a['category']}|{a['title']}|{a['severity']}"
            for r in rs:
                if not aud & set(r["audiences"].split(",")) or SEVERITY[a["severity"]] < SEVERITY[r["min_severity"]]:
                    continue
                if _recently_sent(c, key, r["address"]):
                    skipped += 1
                    continue
                if r["channel"] == "sms":
                    body = f"KhanijSetu {a['severity'].upper()}: {a['title']}. {a['recommended_action']}"[:300]
                else:
                    body = (f"{a['message']}\n\nRecommended action: {a['recommended_action']}\n\n"
                            f"Category: {a['category']} · severity: {a['severity']} · alert {a['alert_id']} · {a['ts']}\n"
                            f"Addressed to: {a['audience'].replace(',', ', ')}\n\n— KhanijSetu early-warning system")
                _queue(c, "alert", a["alert_id"], r, f"[KhanijSetu][{a['severity'].upper()}] {a['title']}", body, dedup_key=key)
                n += 1
    return dict(queued=n, deduplicated=skipped)


def enqueue_report(name, title, path, audiences, summary=""):
    """Reports go by email (attachment) and a one-line SMS heads-up."""
    rs = [r for r in recipients(active_only=True) if set(audiences) & set(r["audiences"].split(","))]
    with _con() as c:
        for r in rs:
            if r["channel"] == "email":
                _queue(c, "report", name, r, f"[KhanijSetu] {title}",
                       f"{summary}\n\nThe full report is attached ({Path(path).name}).\n\n— KhanijSetu scheduled reports", attachment=path)
            else:
                _queue(c, "report", name, r, title, f"KhanijSetu: {title} is ready. {summary}"[:300])
    return len(rs)


def enqueue_test(channel, address):
    init()
    r = dict(channel=channel, name="test", address=address)
    with _con() as c:
        _queue(c, "test", "test", r, "[KhanijSetu] Test message",
               "This is a test message from KhanijSetu. If you received it, delivery is configured correctly.")


# ---- delivery --------------------------------------------------------------------------------
def _send_email(s, m):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s["smtp_from"], m["address"], m["subject"]
    msg.set_content(m["body"])
    if m["attachment"] and Path(m["attachment"]).exists():
        p = Path(m["attachment"])
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        main, sub = ctype.split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=main, subtype=sub, filename=p.name)
    port = int(s["smtp_port"] or 0)
    if s["smtp_security"] == "ssl":
        srv = smtplib.SMTP_SSL(s["smtp_host"], port or 465, timeout=20, context=ssl.create_default_context())
    else:
        srv = smtplib.SMTP(s["smtp_host"], port or 587, timeout=20)
        if s["smtp_security"] == "starttls":
            srv.starttls(context=ssl.create_default_context())
    with srv:
        if s["smtp_user"]:
            srv.login(s["smtp_user"], s.get("smtp_password", ""))
        srv.send_message(msg)


def _send_sms(s, m):
    url = f"{s['sms_base_url'].rstrip('/')}/2010-04-01/Accounts/{s['sms_account_sid']}/Messages.json"
    r = requests.post(url, data=dict(To=m["address"], From=s["sms_from"], Body=m["body"]),
                      auth=(s["sms_account_sid"], s["sms_auth_token"]), timeout=20)
    if r.status_code >= 300:
        raise RuntimeError(f"gateway HTTP {r.status_code}: {r.text[:200]}")


def flush(limit=200):
    """Deliver queued (and retry failed) messages. Unconfigured channels hold their messages."""
    s = _raw_settings()
    ready = dict(email=_email_ready(s), sms=_sms_ready(s))
    stats = dict(sent=0, failed=0, held=0)
    init()
    with _con() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM outbox WHERE status='queued' OR (status='failed' AND attempts<?) OR status='held' ORDER BY id LIMIT ?",
            (MAX_ATTEMPTS, limit))]
    for m in rows:
        if not ready[m["channel"]]:
            if m["status"] != "held":
                with _con() as c:
                    c.execute("UPDATE outbox SET status='held', last_error=? WHERE id=?",
                              (f"{m['channel']} channel not configured", m["id"]))
            stats["held"] += 1
            continue
        try:
            (_send_email if m["channel"] == "email" else _send_sms)(s, m)
            status, err, sent = "sent", None, datetime.now().isoformat(timespec="seconds")
            stats["sent"] += 1
        except Exception as e:  # noqa: BLE001 — record any transport error and retry later
            status, err, sent = "failed", f"{type(e).__name__}: {e}"[:300], None
            stats["failed"] += 1
        with _con() as c:
            c.execute("UPDATE outbox SET status=?, attempts=attempts+1, last_error=?, sent_at=? WHERE id=?",
                      (status, err, sent, m["id"]))
    return stats


def outbox(limit=200, ref=None):
    init()
    with _con() as c:
        q, args = "SELECT * FROM outbox", []
        if ref:
            q, args = q + " WHERE ref=?", [ref]
        rows = [dict(r) for r in c.execute(q + " ORDER BY id DESC LIMIT ?", args + [limit])]
        counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM outbox GROUP BY status")}
    for r in rows:
        r["attachment"] = Path(r["attachment"]).name if r["attachment"] else None
    return dict(messages=rows, counts=counts)


# ---- dev sinks -------------------------------------------------------------------------------
def run_smtp_sink(port=1025):
    """Local SMTP server that stores every message as .eml (for demos without a real relay)."""
    from aiosmtpd.controller import Controller
    out = DATA / "mail_sink"
    out.mkdir(parents=True, exist_ok=True)

    class Handler:
        async def handle_DATA(self, server, session, envelope):
            name = f"{datetime.now():%Y%m%d-%H%M%S-%f}.eml"
            (out / name).write_bytes(envelope.content)
            print(f"stored {name} for {', '.join(envelope.rcpt_tos)}")
            return "250 Message accepted for delivery"

    ctl = Controller(Handler(), hostname="127.0.0.1", port=port)
    ctl.start()
    print(f"SMTP sink on 127.0.0.1:{port} -> {out}  (Ctrl+C to stop)", flush=True)
    try:
        import time
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        ctl.stop()


def record_sms_sink(sid, form):
    rec = dict(ts=datetime.now().isoformat(timespec="seconds"), account=sid, **{k: form.get(k) for k in ("To", "From", "Body")})
    with open(DATA / "sms_sink.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return dict(sid=f"SM{abs(hash(json.dumps(rec))) % 10**30:030d}", status="queued", to=rec["To"], body=rec["Body"])


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "sink":
        run_smtp_sink(int(sys.argv[2]) if len(sys.argv) > 2 else 1025)
    elif len(sys.argv) > 1 and sys.argv[1] == "flush":
        print(flush())
    else:
        print(__doc__)
