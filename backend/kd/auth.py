"""Authentication & role-based access control.

- Users live in data/auth.sqlite (separate from the warehouse, so a pipeline rebuild never wipes accounts).
- Passwords: PBKDF2-HMAC-SHA256, 200k iterations, per-user salt.
- Sessions: stateless signed tokens (HMAC-SHA256 over a JSON payload, 12 h expiry). The role is
  re-read from the user table on every request, so a role change or deletion takes effect immediately.
- Brute-force guard: 5 failed attempts per username in 5 minutes -> 429 for the rest of the window.
- Every login, failure and logout is written to an audit log.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime

from fastapi import Depends, Header, HTTPException, Request

from . import db
from .config import DATA

AUTH_DB = DATA / "auth.sqlite"
SECRET_FILE = DATA / ".secret_key"
TOKEN_TTL_S = 12 * 3600
ITERATIONS = 200_000

PERMISSIONS = {
    "overview": "Command Center dashboard",
    "reserves": "Reserve maps, GIS layers, drill-down, reserve exports",
    "production": "Production history and shortfall forecasts",
    "optimize": "What-if scenario analysis",
    "optimize_run": "Generate prescriptive corrective-action plans",
    "alerts": "View alerts addressed to the role",
    "alerts_ack": "Acknowledge alerts",
    "reports": "Export reports",
    "system": "Data lineage, model cards, pipeline logs",
    "pipeline_run": "Re-run the data/ML pipeline",
    "data_upload": "Upload survey data (boreholes, geochem, aeromag, geology maps)",
    "users": "Manage users, view audit log",
    "models": "Promote / roll back / pin models in the model registry",
    "notify_admin": "Configure Email/SMS delivery, recipients and report schedules",
}
ROLES = {
    "admin": dict(label="System Administrator", perms=list(PERMISSIONS), audience=None,
                  focus="Platform, data pipeline, users and access"),
    "mgmt": dict(label="Management / Board", audience={"management"},
                 perms=["overview", "reserves", "production", "optimize", "optimize_run", "alerts", "reports", "system"],
                 focus="High-level dashboards, forecasts, strategic decisions"),
    "planner": dict(label="Mine Planners & Geologists", audience={"planners", "geology"},
                    perms=["overview", "reserves", "production", "optimize", "alerts", "reports", "system", "data_upload"],
                    focus="Reserve maps, extraction planning, geological layers"),
    "ops": dict(label="Operations Team", audience={"operations"},
                perms=["overview", "production", "optimize", "optimize_run", "alerts", "alerts_ack", "reports"],
                focus="Monitor production, risk alerts, corrective actions"),
}
HOME = {"admin": "/system", "mgmt": "/", "planner": "/reserves", "ops": "/production"}

_failures = {}


def _con():
    return db.connect("auth")


def _secret():
    env = os.environ.get("KD_TOKEN_SECRET") or os.environ.get("KD_SECRET")
    if env:
        return env.encode()
    if not SECRET_FILE.exists():
        DATA.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_bytes(secrets.token_bytes(32))
    return SECRET_FILE.read_bytes()


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    return salt.hex(), hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS).hex()


def _verify(password, salt_hex, hash_hex):
    _, h = hash_password(password, bytes.fromhex(salt_hex))
    return hmac.compare_digest(h, hash_hex)


def init():
    """Create tables and seed either one production admin or the opt-in demo accounts."""
    with _con() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, name TEXT, role TEXT,
                     salt TEXT, hash TEXT, created TEXT, last_login TEXT)""")
        c.execute("CREATE TABLE IF NOT EXISTS audit_log (ts TEXT, username TEXT, event TEXT, ip TEXT, ok INTEGER)")
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            bootstrap = os.environ.get("KD_BOOTSTRAP_ADMIN_PASSWORD")
            if bootstrap:
                initial = [dict(username=os.environ.get("KD_BOOTSTRAP_ADMIN_USERNAME", "admin").strip().lower(),
                                name=os.environ.get("KD_BOOTSTRAP_ADMIN_NAME", "Platform Administrator"),
                                role="admin", password=bootstrap)]
            elif os.environ.get("KD_DEMO_LOGIN", "1") == "1":
                from .seed_users import DEMO_USERS
                initial = DEMO_USERS
            else:
                initial = []
            for u in initial:
                salt, h = hash_password(u["password"])
                c.execute("INSERT INTO users VALUES (?,?,?,?,?,?,NULL)",
                          (u["username"], u["name"], u["role"], salt, h, datetime.now().isoformat(timespec="seconds")))


def _audit(username, event, ip, ok):
    with _con() as c:
        c.execute("INSERT INTO audit_log VALUES (?,?,?,?,?)",
                  (datetime.now().isoformat(timespec="seconds"), username, event, ip, int(ok)))


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(user):
    payload = _b64(json.dumps(dict(sub=user["username"], role=user["role"], iat=int(time.time()),
                                   exp=int(time.time()) + TOKEN_TTL_S)).encode())
    sig = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def public_user(row):
    r = ROLES[row["role"]]
    return dict(username=row["username"], name=row["name"], role=row["role"], role_label=r["label"],
                focus=r["focus"], perms=r["perms"], home=HOME[row["role"]], last_login=row["last_login"])


def login(username, password, ip):
    username = (username or "").strip().lower()
    now = time.time()
    recent = [t for t in _failures.get(username, []) if now - t < 300]
    _failures[username] = recent
    if len(recent) >= 5:
        _audit(username, "login_locked", ip, False)
        raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")
    with _con() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row or not _verify(password or "", row["salt"], row["hash"]):
            _failures[username].append(now)
            _audit(username, "login_failed", ip, False)
            raise HTTPException(401, "Invalid username or password.")
        prev = row["last_login"]
        c.execute("UPDATE users SET last_login=? WHERE username=?", (datetime.now().isoformat(timespec="seconds"), username))
    _failures.pop(username, None)
    _audit(username, "login", ip, True)
    user = public_user(row)
    user["last_login"] = prev
    return dict(token=issue_token(row), expires_in=TOKEN_TTL_S, user=user)


def current_user(request: Request, authorization: str | None = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Sign in required.")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload, sig = token.split(".")
        good = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            raise ValueError
        data = json.loads(_unb64(payload))
    except Exception:
        raise HTTPException(401, "Invalid session.")
    if data.get("exp", 0) < time.time():
        raise HTTPException(401, "Session expired. Please sign in again.")
    with _con() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (data["sub"],)).fetchone()
    if not row:
        raise HTTPException(401, "Account no longer exists.")
    return public_user(row)


def require(*perms):
    """FastAPI dependency: the signed-in user must hold every listed permission."""
    def dep(user=Depends(current_user)):
        missing = [p for p in perms if p not in user["perms"]]
        if missing:
            raise HTTPException(403, f"Your role ({user['role_label']}) can't access this: needs {', '.join(missing)}.")
        return user
    return dep


def can(user, perm):
    return perm in user["perms"]


def alert_audience(user):
    return ROLES[user["role"]]["audience"]


def logout(user, ip):
    _audit(user["username"], "logout", ip, True)


def audit(username, event, detail=""):
    """Administrative actions (model promotions, unpins) in the same audit log as sign-ins."""
    _audit(username, f"{event} {detail}".strip(), "-", True)


def users_and_audit():
    with _con() as c:
        users = [dict(username=r["username"], name=r["name"], role=r["role"], role_label=ROLES[r["role"]]["label"],
                      created=r["created"], last_login=r["last_login"])
                 for r in c.execute("SELECT * FROM users ORDER BY role, username")]
        audit = [dict(r) for r in c.execute("SELECT * FROM audit_log ORDER BY ts DESC LIMIT 60")]
    return dict(users=users, audit=audit, roles={k: dict(label=v["label"], perms=v["perms"], focus=v["focus"]) for k, v in ROLES.items()},
                permissions=PERMISSIONS)


def demo_accounts():
    """Demo credentials for the login page. Disable with KD_DEMO_LOGIN=0 for a real deployment."""
    if os.environ.get("KD_DEMO_LOGIN", "1") != "1":
        return []
    from .seed_users import DEMO_USERS
    return [dict(username=u["username"], password=u["password"], name=u["name"], role=u["role"],
                 role_label=ROLES[u["role"]]["label"], focus=ROLES[u["role"]]["focus"]) for u in DEMO_USERS]
