"""Layer 2 · Data Ingestion for mine operations (diagram: "IoT / Equipment Data", "APIs").

One validation path for every operational record, whatever the channel:
  • devices    POST /api/ingest/telemetry   (X-Device-Key; e.g. fleet telematics, shift loggers)
  • uploads    CSV/XLSX through the upload UI (MIS exports)
  • backfill   the virtual-mine service seeding history

Records land in the persistent operational store (data/ops.sqlite, never wiped by pipeline
rebuilds), raw batches are archived to the data lake as JSONL, and anything invalid goes to a
dead-letter table with the reason. Writes are idempotent upserts on each record's natural key,
so a device can safely retry a batch.
"""
import hashlib
import json
import secrets
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from . import db
from .config import DATA, LAKE, MINE_BY_ID

OPS = DATA / "ops.sqlite"
ARCHIVE = LAKE / "ops"

SCHEMA = """
CREATE TABLE IF NOT EXISTS production_shift (date TEXT, mine_id TEXT, shift TEXT, tonnes REAL, source TEXT, batch_id TEXT, ingested_at TEXT,
  PRIMARY KEY (date, mine_id, shift));
CREATE TABLE IF NOT EXISTS equipment_daily (date TEXT, mine_id TEXT, dumpers_total INTEGER, dumpers_available INTEGER,
  excavator_availability_pct REAL, downtime_hrs REAL, source TEXT, batch_id TEXT, ingested_at TEXT, PRIMARY KEY (date, mine_id));
CREATE TABLE IF NOT EXISTS blasting (date TEXT, mine_id TEXT, blasts INTEGER, explosive_kg REAL, source TEXT, batch_id TEXT, ingested_at TEXT,
  PRIMARY KEY (date, mine_id));
CREATE TABLE IF NOT EXISTS manpower (date TEXT, mine_id TEXT, rostered INTEGER, present INTEGER, source TEXT, batch_id TEXT, ingested_at TEXT,
  PRIMARY KEY (date, mine_id));
CREATE TABLE IF NOT EXISTS plan (month TEXT, mine_id TEXT, target_t REAL, source TEXT, batch_id TEXT, ingested_at TEXT, PRIMARY KEY (month, mine_id));
CREATE TABLE IF NOT EXISTS heartbeat (ts TEXT, device_id TEXT, mine_id TEXT, equipment_id TEXT, kind TEXT, status TEXT,
  utilisation_pct REAL, payload_t REAL, fuel_pct REAL, batch_id TEXT, PRIMARY KEY (equipment_id, ts));
CREATE INDEX IF NOT EXISTS heartbeat_ts ON heartbeat (ts);
CREATE TABLE IF NOT EXISTS devices (device_id TEXT PRIMARY KEY, mine_id TEXT, label TEXT, key_hash TEXT, created TEXT, last_seen TEXT, active INTEGER);
CREATE TABLE IF NOT EXISTS batches (batch_id TEXT PRIMARY KEY, ts TEXT, channel TEXT, source TEXT, device_id TEXT, n_ok INTEGER, n_bad INTEGER);
CREATE TABLE IF NOT EXISTS dead_letter (ts TEXT, batch_id TEXT, channel TEXT, source TEXT, record TEXT, error TEXT);
"""


def _con():
    return db.connect("ops")


def init():
    with _con() as c:
        c.executescript(SCHEMA)


# ---- record schemas -------------------------------------------------------------------------
class _Base(BaseModel):
    mine_id: str

    @field_validator("mine_id")
    @classmethod
    def known_mine(cls, v):
        if v not in MINE_BY_ID:
            raise ValueError(f"unknown mine_id '{v}'")
        return v


class _Daily(_Base):
    date: date

    @field_validator("date")
    @classmethod
    def not_future(cls, v):
        if v > date.today():
            raise ValueError("date is in the future")
        return v


class Production(_Daily):
    type: Literal["production"] = "production"
    shift: Literal["A", "B", "C"]
    tonnes: float = Field(ge=0, le=20_000)


class Equipment(_Daily):
    type: Literal["equipment"] = "equipment"
    dumpers_total: int = Field(ge=0, le=200)
    dumpers_available: int = Field(ge=0, le=200)
    excavator_availability_pct: float = Field(ge=0, le=100)
    downtime_hrs: float = Field(ge=0, le=24 * 20)


class Blasting(_Daily):
    type: Literal["blasting"] = "blasting"
    blasts: int = Field(ge=0, le=20)
    explosive_kg: float = Field(ge=0, le=50_000)


class Manpower(_Daily):
    type: Literal["manpower"] = "manpower"
    rostered: int = Field(ge=0, le=5000)
    present: int = Field(ge=0, le=5000)


class Plan(_Base):
    type: Literal["plan"] = "plan"
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    target_t: float = Field(ge=0, le=2_000_000)


class Heartbeat(_Base):
    type: Literal["heartbeat"] = "heartbeat"
    ts: datetime
    equipment_id: str
    kind: Literal["excavator", "dumper", "drill", "loader", "hoist"]
    status: Literal["working", "idle", "breakdown", "maintenance"]
    utilisation_pct: float = Field(ge=0, le=100)
    payload_t: float | None = Field(default=None, ge=0, le=500)
    fuel_pct: float | None = Field(default=None, ge=0, le=100)


MODELS = {m.model_fields["type"].default: m for m in (Production, Equipment, Blasting, Manpower, Plan, Heartbeat)}
TABLE = dict(production="production_shift", equipment="equipment_daily", blasting="blasting", manpower="manpower", plan="plan")


def _validate(rec):
    t = rec.get("type")
    if t not in MODELS:
        raise ValueError(f"unknown record type '{t}' (expected one of {', '.join(MODELS)})")
    obj = MODELS[t].model_validate(rec)
    if t == "equipment" and obj.dumpers_available > obj.dumpers_total:
        raise ValueError("dumpers_available exceeds dumpers_total")
    if t == "manpower" and obj.present > obj.rostered * 1.2:
        raise ValueError("present exceeds rostered by more than 20%")
    return obj


# ---- accept a batch -------------------------------------------------------------------------
def accept(records, channel, source, device_id=None, archive=True, allowed_mine=None):
    """Validate + upsert a batch. Returns a summary; never raises on bad records."""
    init()
    now = datetime.now().isoformat(timespec="seconds")
    batch_id = f"{datetime.now():%Y%m%d%H%M%S%f}-{secrets.token_hex(3)}"
    good, bad = {}, []
    for r in records:
        try:
            o = _validate(r)
            if allowed_mine and o.mine_id != allowed_mine:
                raise ValueError(f"device is registered to {allowed_mine}, not {o.mine_id}")
            good.setdefault(o.type, []).append(o)
        except (ValidationError, ValueError) as e:
            msg = e.errors()[0]["msg"] if isinstance(e, ValidationError) else str(e)
            loc = ".".join(str(x) for x in e.errors()[0]["loc"]) if isinstance(e, ValidationError) else ""
            bad.append(dict(ts=now, batch_id=batch_id, channel=channel, source=source,
                            record=json.dumps(r, default=str)[:2000], error=f"{loc}: {msg}" if loc else msg))
    with _con() as c:
        for t, objs in good.items():
            if t == "heartbeat":
                # live status only: keep 24 h of heartbeats (daily equipment records are the history)
                c.execute("DELETE FROM heartbeat WHERE ts < ?", ((datetime.now() - timedelta(days=1)).isoformat(timespec="seconds"),))
                c.executemany("INSERT OR REPLACE INTO heartbeat VALUES (?,?,?,?,?,?,?,?,?,?)",
                              [(o.ts.isoformat(timespec="seconds"), device_id or source, o.mine_id, o.equipment_id, o.kind, o.status,
                                o.utilisation_pct, o.payload_t, o.fuel_pct, batch_id) for o in objs])
                continue
            fields = [f for f in MODELS[t].model_fields if f != "type"]
            rows = [tuple(str(getattr(o, f)) if f in ("date",) else getattr(o, f) for f in fields) + (source, batch_id, now) for o in objs]
            cols = fields + ["source", "batch_id", "ingested_at"]
            c.executemany(f"INSERT OR REPLACE INTO {TABLE[t]} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", rows)
        if bad:
            c.executemany("INSERT INTO dead_letter VALUES (:ts,:batch_id,:channel,:source,:record,:error)", bad)
        n_ok = sum(len(v) for v in good.values())
        c.execute("INSERT INTO batches VALUES (?,?,?,?,?,?,?)", (batch_id, now, channel, source, device_id, n_ok, len(bad)))
        if device_id:
            c.execute("UPDATE devices SET last_seen=? WHERE device_id=?", (now, device_id))
    if archive and records:
        day = ARCHIVE / now[:10]
        day.mkdir(parents=True, exist_ok=True)
        with open(day / f"{channel}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(batch_id=batch_id, ts=now, source=source, device_id=device_id, records=records), default=str) + "\n")
    return dict(batch_id=batch_id, accepted=n_ok, rejected=len(bad), errors=[b["error"] for b in bad[:10]])


# ---- devices (API keys) ---------------------------------------------------------------------
def _hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def create_device(device_id, mine_id, label=""):
    """Returns the plaintext key ONCE; only its hash is stored."""
    init()
    if mine_id not in MINE_BY_ID:
        raise ValueError("unknown mine")
    key = "kdk_" + secrets.token_urlsafe(24)
    with _con() as c:
        c.execute("INSERT OR REPLACE INTO devices VALUES (?,?,?,?,?,NULL,1)",
                  (device_id, mine_id, label, _hash(key), datetime.now().isoformat(timespec="seconds")))
    return key


def device_for_key(key):
    if not key:
        return None
    init()
    with _con() as c:
        r = c.execute("SELECT * FROM devices WHERE key_hash=? AND active=1", (_hash(key),)).fetchone()
    return dict(r) if r else None


def revoke_device(device_id):
    with _con() as c:
        c.execute("UPDATE devices SET active=0 WHERE device_id=?", (device_id,))


# ---- reads ----------------------------------------------------------------------------------
def table(name):
    import pandas as pd
    init()
    with _con() as c:
        return pd.read_sql(f"SELECT * FROM {name}", c)


def count(name):
    init()
    with _con() as c:
        return c.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]


def status():
    init()
    with _con() as c:
        q = lambda sql, *a: [dict(r) for r in c.execute(sql, a)]
        return dict(
            counts={t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("production_shift", "equipment_daily", "blasting", "manpower", "plan", "heartbeat", "dead_letter")},
            latest_date=c.execute("SELECT MAX(date) FROM production_shift").fetchone()[0],
            devices=q("SELECT device_id, mine_id, label, created, last_seen, active FROM devices ORDER BY mine_id, device_id"),
            batches=q("SELECT * FROM batches ORDER BY ts DESC LIMIT 25"),
            by_channel=q("SELECT channel, COUNT(*) AS batches, SUM(n_ok) AS ok, SUM(n_bad) AS bad, MAX(ts) AS last FROM batches GROUP BY channel"),
            dead_letter=q("SELECT * FROM dead_letter ORDER BY ts DESC LIMIT 25"),
        )


def live_equipment(minutes=30):
    """Latest heartbeat per machine within the window."""
    init()
    with _con() as c:
        rows = c.execute("""SELECT h.* FROM heartbeat h JOIN (SELECT equipment_id, MAX(ts) AS ts FROM heartbeat GROUP BY equipment_id) m
                            ON h.equipment_id=m.equipment_id AND h.ts=m.ts
                            WHERE h.ts >= ? ORDER BY h.mine_id, h.kind, h.equipment_id""",
                         ((datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds"),)).fetchall()
    return [dict(r) for r in rows]


def snapshot_to_lake():
    """Export the operational store as the 'MIS export' files the pipeline ingests (data lake),
    with provenance describing where the records came from."""
    out = LAKE / "production"
    out.mkdir(parents=True, exist_ok=True)
    files = dict(production_shift=("mis_shift_production", ["date", "mine_id", "shift", "tonnes"]),
                 equipment_daily=("equipment_telemetry", ["date", "mine_id", "dumpers_total", "dumpers_available",
                                                          "excavator_availability_pct", "downtime_hrs"]),
                 blasting=("blasting_register", ["date", "mine_id", "blasts", "explosive_kg"]),
                 manpower=("manpower_attendance", ["date", "mine_id", "rostered", "present"]),
                 plan=("monthly_plan", ["month", "mine_id", "target_t"]))
    st = status()
    channels = {c["channel"]: c["ok"] for c in st["by_channel"]}
    for t, (name, cols) in files.items():
        df = table(t)
        df[cols].sort_values(cols[:2]).to_csv(out / f"{name}.csv", index=False)
        mix = df.source.map(lambda v: "virtual-mine" if str(v).startswith("vm-") or v == "virtual-mine" else v).value_counts()
        virtual = int(mix.get("virtual-mine", 0))
        # REAL only when no record came from the virtual mine; a mixed store says so explicitly
        (out / f"{name}.provenance.json").write_text(json.dumps(dict(
            name=name, real=bool(len(df)) and virtual == 0,
            source=("Ingestion API · virtual-mine devices (calibrated to MOIL published company totals)" if virtual == len(df)
                    else "Ingestion API · " + ", ".join(f"{k} ({v})" for k, v in mix.items())),
            method=f"operational store snapshot; channels {channels}; dead-lettered {st['counts']['dead_letter']}",
            date_range=f"{df.iloc[:, 0].min()}/{df.iloc[:, 0].max()}" if len(df) else None, records=len(df),
            fetched_at=datetime.now().isoformat(timespec="seconds")), indent=2))
    return {k: count(k) for k in files}


OFFLINE_AFTER_MIN = 10


def equipment_status(mine_id, hours=24, bucket_min=15):
    """Fleet status for one mine: latest heartbeat per machine (online if seen in the last
    OFFLINE_AFTER_MIN minutes), a per-machine timeline of the dominant status per bucket, and the
    daily availability log for the last 30 days."""
    from datetime import datetime, timedelta
    init()
    now = datetime.now()
    since = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    with _con() as c:
        latest = [dict(r) for r in c.execute(
            """SELECT h.* FROM heartbeat h JOIN (SELECT equipment_id, MAX(ts) AS ts FROM heartbeat WHERE mine_id=? AND ts>=?
               GROUP BY equipment_id) m ON h.equipment_id=m.equipment_id AND h.ts=m.ts ORDER BY h.kind, h.equipment_id""",
            (mine_id, since))]
        # portable bucketing (SQLite + PostgreSQL): hour prefix + minute bucket, assembled in Python
        raw = c.execute(
            f"""SELECT equipment_id, SUBSTR(ts, 1, 14) AS h, CAST(SUBSTR(ts, 15, 2) AS INTEGER) / {bucket_min} AS q,
                       status, COUNT(*) AS n
                FROM heartbeat WHERE mine_id=? AND ts>=? GROUP BY equipment_id, SUBSTR(ts, 1, 14),
                CAST(SUBSTR(ts, 15, 2) AS INTEGER) / {bucket_min}, status""", (mine_id, since)).fetchall()
        buckets = [dict(equipment_id=r["equipment_id"], b=f"{r['h']}{int(r['q']) * bucket_min:02d}", status=r["status"], n=r["n"])
                   for r in raw]
        daily = [dict(r) for r in c.execute(
            "SELECT date, excavator_availability_pct, dumpers_available, dumpers_total, downtime_hrs, source FROM equipment_daily "
            "WHERE mine_id=? ORDER BY date DESC LIMIT 30", (mine_id,))][::-1]
    # timeline: dominant status per bucket
    start = now - timedelta(hours=hours)
    start = start.replace(minute=(start.minute // bucket_min) * bucket_min, second=0, microsecond=0)
    keys = [(start + timedelta(minutes=bucket_min * k)).strftime("%Y-%m-%dT%H:%M") for k in range(hours * 60 // bucket_min + 1)]
    best = {}
    for r in buckets:
        k = (r["equipment_id"], r["b"])
        if k not in best or r["n"] > best[k][1]:
            best[k] = (r["status"], r["n"])
    code = {"working": "w", "idle": "i", "maintenance": "m", "breakdown": "b"}
    for m in latest:
        age = (now - datetime.fromisoformat(m["ts"])).total_seconds() / 60
        m["online"] = age <= OFFLINE_AFTER_MIN
        m["age_min"] = round(age, 1)
        tl = "".join(code.get(best.get((m["equipment_id"], k), ("-",))[0], "-") for k in keys)
        m["timeline"] = tl
        seen = [ch for ch in tl if ch != "-"]
        m["availability_24h"] = round(sum(ch in "wi" for ch in seen) / len(seen), 3) if seen else None
        m["breakdowns_24h"] = sum(1 for a, b in zip("-" + tl, tl) if b == "b" and a != "b")
    online = [m for m in latest if m["online"]]
    summary = dict(total=len(latest), online=len(online), **{s: sum(m["status"] == s for m in online) for s in code})
    summary["availability_now"] = round((summary["working"] + summary["idle"]) / len(online), 3) if online else None
    summary["utilisation_now"] = round(sum(m["utilisation_pct"] for m in online if m["status"] == "working") / max(summary["working"], 1), 1) if online else None
    summary["last_seen"] = max((m["ts"] for m in latest), default=None)
    return dict(mine_id=mine_id, as_of=now.isoformat(timespec="seconds"), hours=hours, bucket_min=bucket_min,
                timeline_start=keys[0], machines=latest, summary=summary, daily=daily, offline_after_min=OFFLINE_AFTER_MIN)
