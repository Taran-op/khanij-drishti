"""Job queue: long-running work (pipeline runs, reserve re-maps, backups, restore drills) as rows in
the 'jobs' store, executed by a worker.

    API / scheduler  ──enqueue──▶  jobs (queued) ──claim──▶ worker ──▶ done | error
                                                   (UPDATE … WHERE status='queued': one worker wins)

In a single-process deployment the API runs an in-process worker thread (KD_WORKER=inprocess, the
default). In Docker/Kubernetes the API only enqueues (KD_WORKER=external) and `python -m kd.jobs`
runs in the worker container. Log lines are appended to the job row, so any API replica can show
progress.
"""
import json
import os
import socket
import threading
import time
import traceback
from datetime import datetime, timedelta

from . import db

KINDS = ("pipeline", "reserve_remap", "backup", "restore_drill", "log_forecasts")
STALE_AFTER = timedelta(hours=3)


def _now():
    return datetime.now().isoformat(timespec="seconds")


def init():
    with db.connect("jobs") as c:
        c.execute("""CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, params TEXT, status TEXT,
                     requested_by TEXT, created TEXT, started TEXT, finished TEXT, worker TEXT, log TEXT, error TEXT, result TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, id)")


def enqueue(kind, params=None, by="system"):
    """Queue a job; if one of the same kind is already queued or running, return that one instead."""
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind}")
    init()
    with db.connect("jobs") as c:
        r = c.execute("SELECT id FROM jobs WHERE kind=? AND status IN ('queued', 'running') ORDER BY id LIMIT 1", (kind,)).fetchone()
        if r:
            return r[0], False
        cur = c.execute("INSERT INTO jobs (kind, params, status, requested_by, created, log) VALUES (?, ?, 'queued', ?, ?, '')",
                        (kind, json.dumps(params or {}), by, _now()))
        return cur.lastrowid, True


def claim(worker):
    """Atomically take the oldest queued job. Returns the job dict or None."""
    with db.connect("jobs") as c:
        r = c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if not r:
            return None
        got = c.execute("UPDATE jobs SET status='running', started=?, worker=? WHERE id=? AND status='queued'",
                        (_now(), worker, r[0])).rowcount
    return get(r[0]) if got else None


def append_log(job_id, line):
    with db.connect("jobs") as c:
        c.execute("UPDATE jobs SET log = COALESCE(log, '') || ? WHERE id=?", (str(line).rstrip("\n") + "\n", job_id))


def finish(job_id, status, error=None, result=None):
    with db.connect("jobs") as c:
        c.execute("UPDATE jobs SET status=?, finished=?, error=?, result=? WHERE id=?",
                  (status, _now(), error, json.dumps(result, default=str) if result is not None else None, job_id))


def get(job_id):
    init()
    with db.connect("jobs") as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _view(dict(r)) if r else None


def _view(j):
    j["log_lines"] = (j.get("log") or "").splitlines()
    j["params"] = json.loads(j["params"] or "{}")
    j["result"] = json.loads(j["result"]) if j.get("result") else None
    j.pop("log", None)
    return j


def recent(limit=20):
    init()
    with db.connect("jobs") as c:
        return [_view(dict(r)) for r in c.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))]


def latest(kind=None):
    init()
    with db.connect("jobs") as c:
        q, a = ("SELECT * FROM jobs WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,)) if kind else ("SELECT * FROM jobs ORDER BY id DESC LIMIT 1", ())
        r = c.execute(q, a).fetchone()
    return _view(dict(r)) if r else None


def last_finished_id():
    init()
    with db.connect("jobs") as c:
        r = c.execute("SELECT MAX(id) FROM jobs WHERE status IN ('done', 'error')").fetchone()
    return r[0] or 0


def counts():
    init()
    with db.connect("jobs") as c:
        return {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}


def recover_stale():
    """A worker that died mid-job leaves it 'running'; mark such jobs failed so they can be re-queued."""
    cutoff = (datetime.now() - STALE_AFTER).isoformat(timespec="seconds")
    with db.connect("jobs") as c:
        return c.execute("UPDATE jobs SET status='error', finished=?, error='worker lost (stale)' WHERE status='running' AND started < ?",
                         (_now(), cutoff)).rowcount


# ---- execution -------------------------------------------------------------------------------
def execute(job):
    from . import pipeline
    log = lambda line: append_log(job["id"], line)  # noqa: E731
    k, p = job["kind"], job["params"]
    if k == "pipeline":
        pipeline.run(log=log)
        return None
    if k == "reserve_remap":
        from . import reserve
        log("re-mapping reserves with the current champion")
        card, zones = reserve.train_and_map()
        log(f"{len(zones)} zones · champion {card.get('champion')}")
        return dict(zones=len(zones), champion=card.get("champion"))
    if k == "backup":
        from . import backup
        return backup.create(include_lake=bool(p.get("include_lake")), log=log)
    if k == "restore_drill":
        from . import backup
        return backup.drill(p.get("name"), log=log)
    if k == "log_forecasts":
        from . import monitoring
        return monitoring.log_forecasts()
    raise ValueError(f"unknown job kind {k}")


def run_one(worker):
    job = claim(worker)
    if not job:
        return False
    t = time.time()
    try:
        result = execute(job)
        append_log(job["id"], f"done in {time.time() - t:.0f}s")
        finish(job["id"], "done", result=result)
    except Exception as e:  # noqa: BLE001 — recorded on the job; the worker keeps serving
        traceback.print_exc()
        append_log(job["id"], f"ERROR {type(e).__name__}: {e}")
        finish(job["id"], "error", error=f"{type(e).__name__}: {e}"[:500])
    return True


def worker_loop(poll=3, stop=None, name=None):
    name = name or f"{socket.gethostname()}:{os.getpid()}"
    init()
    recover_stale()
    while not (stop and stop.is_set()):
        try:
            if not run_one(name):
                (stop.wait(poll) if stop else time.sleep(poll))
        except Exception:  # noqa: BLE001 — e.g. database restarting; retry
            traceback.print_exc()
            time.sleep(poll * 2)


_inproc = dict(thread=None, stop=None)


def start_inprocess_worker():
    """Single-process mode: the API runs the worker on a daemon thread."""
    if _inproc["thread"] and _inproc["thread"].is_alive():
        return
    stop = threading.Event()
    t = threading.Thread(target=worker_loop, kwargs=dict(poll=2, stop=stop, name=f"api-inprocess:{os.getpid()}"),
                         daemon=True, name="kd-worker")
    t.start()
    _inproc.update(thread=t, stop=stop)


def external():
    return os.environ.get("KD_WORKER", "inprocess") == "external"


if __name__ == "__main__":
    print("KhanijDrishti worker: polling the job queue (Ctrl+C to stop)", flush=True)
    worker_loop()
