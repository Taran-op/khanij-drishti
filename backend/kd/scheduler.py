"""Layer 5 · Scheduler: periodic reports, data refresh and notification delivery.

Runs as a background thread inside the API process (or standalone: `python -m kd.scheduler`, the
Phase 6 'scheduler' container). Schedules live in data/scheduler.sqlite:

  kind      'report' (generate + archive + email to the audiences) or 'pipeline' (full data/ML refresh)
  every     'daily' | 'weekly' (weekday 0=Mon) | 'monthly' (day of month) at HH:MM local time
  claimed   next_run is advanced with a compare-and-set UPDATE before a job starts, so several API
            workers / a separate scheduler process never run the same occurrence twice.

Every minute the outbox is flushed (queued + retryable messages delivered).
"""
import json
import threading
import time
import traceback
from datetime import datetime, timedelta

from . import db
from .config import DATA

DB = DATA / "scheduler.sqlite"
SEED = [
    dict(name="Daily operations brief", kind="report", report="ops_brief", formats="pdf", every="daily", at="07:00",
         audiences="operations,management"),
    dict(name="Weekly management summary", kind="report", report="management", formats="pdf,xlsx", every="weekly", weekday=0,
         at="08:00", audiences="management"),
    dict(name="Monthly reserve & exploration report", kind="report", report="reserves", formats="pdf,xlsx", every="monthly",
         monthday=1, at="09:00", audiences="planners,geology,management"),
    dict(name="Daily data & model refresh", kind="pipeline", every="daily", at="06:15", audiences=""),
    dict(name="Nightly backup", kind="backup", every="daily", at="02:30", audiences=""),
    dict(name="Weekly restore drill", kind="restore_drill", every="weekly", weekday=6, at="03:30", audiences=""),
]
JOB_KINDS = ("pipeline", "backup", "restore_drill")  # run by the worker through the job queue
_state = dict(thread=None, stop=False, hooks={}, last_tick=None, running=None)


def _con():
    return db.connect("scheduler")


def init():
    with _con() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS schedules (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, kind TEXT,
                     report TEXT, formats TEXT, every TEXT, weekday INTEGER, monthday INTEGER, at TEXT, audiences TEXT,
                     active INTEGER DEFAULT 1, next_run TEXT, last_run TEXT, last_status TEXT, last_detail TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_id INTEGER, name TEXT,
                     started TEXT, finished TEXT, status TEXT, detail TEXT, trigger TEXT)""")
        have = {r[0] for r in c.execute("SELECT name FROM schedules")}
        for s in SEED:  # seed defaults that don't exist yet (also adds new defaults to older installs)
            if s["name"] not in have:
                _insert(c, s)


def next_after(s, after):
    hh, mm = map(int, (s.get("at") or "07:00").split(":"))
    cand = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
    for _ in range(400):
        if cand > after:
            if s["every"] == "daily":
                return cand
            if s["every"] == "weekly" and cand.weekday() == int(s.get("weekday") or 0):
                return cand
            if s["every"] == "monthly" and cand.day == int(s.get("monthday") or 1):
                return cand
        cand += timedelta(days=1)
    raise ValueError("no next run found")


def _validate(s):
    if s.get("kind") not in ("report",) + JOB_KINDS:
        raise ValueError(f"kind must be one of report, {', '.join(JOB_KINDS)}")
    if s.get("every") not in ("daily", "weekly", "monthly"):
        raise ValueError("every must be daily, weekly or monthly")
    hh, mm = map(int, str(s.get("at", "")).split(":"))
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError("at must be HH:MM")
    if s["kind"] == "report":
        from .reports import REPORTS
        if s.get("report") not in REPORTS:
            raise ValueError(f"report must be one of {list(REPORTS)}")
        if not set((s.get("formats") or "pdf").split(",")) <= {"pdf", "xlsx"}:
            raise ValueError("formats must be pdf and/or xlsx")
    if s["every"] == "weekly" and not 0 <= int(s.get("weekday") or 0) <= 6:
        raise ValueError("weekday 0-6")
    if s["every"] == "monthly" and not 1 <= int(s.get("monthday") or 1) <= 28:
        raise ValueError("monthday 1-28")


def _insert(c, s):
    _validate(s)
    nxt = next_after(s, datetime.now()).isoformat(timespec="seconds")
    cur = c.execute("""INSERT INTO schedules (name, kind, report, formats, every, weekday, monthday, at, audiences, active, next_run)
                       VALUES (?,?,?,?,?,?,?,?,?,1,?)""",
                    (s["name"], s["kind"], s.get("report"), s.get("formats", "pdf"), s["every"], s.get("weekday"), s.get("monthday"),
                     s["at"], s.get("audiences", ""), nxt))
    return cur.lastrowid


def add(s):
    init()
    with _con() as c:
        return _insert(c, s)


def update(sid, changes):
    init()
    with _con() as c:
        row = c.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
        if not row:
            raise KeyError(sid)
        s = dict(row) | {k: v for k, v in changes.items() if k in
                         ("name", "report", "formats", "every", "weekday", "monthday", "at", "audiences", "active")}
        _validate(s)
        s["next_run"] = next_after(s, datetime.now()).isoformat(timespec="seconds")
        c.execute("""UPDATE schedules SET name=?, report=?, formats=?, every=?, weekday=?, monthday=?, at=?, audiences=?,
                     active=?, next_run=? WHERE id=?""",
                  (s["name"], s["report"], s["formats"], s["every"], s["weekday"], s["monthday"], s["at"], s["audiences"],
                   int(bool(s["active"])), s["next_run"], sid))


def delete(sid):
    with _con() as c:
        c.execute("DELETE FROM schedules WHERE id=?", (sid,))


def listing():
    init()
    with _con() as c:
        sch = [dict(r) for r in c.execute("SELECT * FROM schedules ORDER BY kind DESC, id")]
        runs = [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 40")]
    return dict(schedules=sch, runs=runs, alive=bool(_state["thread"] and _state["thread"].is_alive()),
                last_tick=_state["last_tick"], running=_state["running"])


# ---- execution -------------------------------------------------------------------------------
def execute(s, trigger="schedule", log=print):
    """Run one schedule now. Returns (status, detail)."""
    from . import notify, reports
    started = datetime.now().isoformat(timespec="seconds")
    _state["running"] = s["name"]
    try:
        if s["kind"] in JOB_KINDS:
            from . import jobs
            jid, new = jobs.enqueue(s["kind"], by=f"schedule: {s['name']}")
            status, detail = "ok", f"job #{jid} {'queued' if new else 'already queued/running'} for the worker"
        else:
            entries, doc = reports.generate(s["report"], tuple((s["formats"] or "pdf").split(",")), by=f"schedule: {s['name']}")
            auds = [a for a in (s["audiences"] or "").split(",") if a]
            n = 0
            for e in entries:
                if e["format"] == "pdf" or len(entries) == 1:
                    n = notify.enqueue_report(s["report"], f"{doc['title']} · {doc.get('subtitle', '')}", e["path"], auds,
                                              reports.summary_line(doc))
            q = notify.flush()
            status = "ok"
            detail = f"{', '.join(e['format'] for e in entries)} archived; {n} recipient(s); delivery {q}"
    except Exception as e:  # noqa: BLE001 — recorded on the run, the loop keeps going
        status, detail = "error", f"{type(e).__name__}: {e}"
        traceback.print_exc()
    finally:
        _state["running"] = None
    with _con() as c:
        c.execute("INSERT INTO runs (schedule_id, name, started, finished, status, detail, trigger) VALUES (?,?,?,?,?,?,?)",
                  (s.get("id"), s["name"], started, datetime.now().isoformat(timespec="seconds"), status, detail[:500], trigger))
        if s.get("id"):
            c.execute("UPDATE schedules SET last_run=?, last_status=?, last_detail=? WHERE id=?", (started, status, detail[:500], s["id"]))
    return status, detail


def run_now(sid):
    init()
    with _con() as c:
        row = c.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
    if not row:
        raise KeyError(sid)
    return execute(dict(row), trigger="manual")


def tick(now=None):
    """Claim and run every due schedule (compare-and-set on next_run), then flush the outbox."""
    from . import notify
    now = now or datetime.now()
    _state["last_tick"] = now.isoformat(timespec="seconds")
    init()
    with _con() as c:
        due = [dict(r) for r in c.execute("SELECT * FROM schedules WHERE active=1 AND next_run<=?", (now.isoformat(timespec="seconds"),))]
    for s in due:
        nxt = next_after(s, now).isoformat(timespec="seconds")
        with _con() as c:
            claimed = c.execute("UPDATE schedules SET next_run=? WHERE id=? AND next_run=?", (nxt, s["id"], s["next_run"])).rowcount
        if claimed:
            execute(s)
    try:
        notify.flush()
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def _loop(every):
    while not _state["stop"]:
        try:
            tick()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        for _ in range(every):
            if _state["stop"]:
                return
            time.sleep(1)


def start(every=30, hooks=None):
    if _state["thread"] and _state["thread"].is_alive():
        return
    _state.update(stop=False, hooks=hooks or {})
    init()
    t = threading.Thread(target=_loop, args=(every,), daemon=True, name="kd-scheduler")
    t.start()
    _state["thread"] = t


def stop():
    _state["stop"] = True


if __name__ == "__main__":
    print("KhanijDrishti scheduler (Ctrl+C to stop)")
    init()
    print(json.dumps(listing()["schedules"], indent=1, default=str))
    _loop(30)
