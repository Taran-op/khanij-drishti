"""Schema migrations and SQLite → PostgreSQL import.

    python -m kd.migrate                 apply pending migrations (idempotent; run on every start)
    python -m kd.migrate status          applied / pending
    python -m kd.migrate import-sqlite [DIR] [--force]
                                         copy every store from SQLite files (default: data/) into the
                                         configured PostgreSQL database (KD_DATABASE_URL); refuses to
                                         overwrite non-empty persistent stores unless --force

Migrations are recorded in meta.schema_migrations. On PostgreSQL an advisory lock serialises
concurrent starters (several API replicas + worker + scheduler booting together).
"""
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import db
from .config import DATA


def _m1_persistent_stores():
    from . import auth, ingest, jobs, monitoring, notify, scheduler
    auth.init(); ingest.init(); notify.init(); scheduler.init(); jobs.init(); monitoring.init()


def _m2_postgis():
    if not db.is_pg():
        return "skipped (sqlite)"
    with db.connect("geo") as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS postgis SCHEMA public")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_features (layer TEXT, fid INTEGER, props JSONB,
                     geom geometry(Geometry, 4326), PRIMARY KEY (layer, fid))""")
        c.execute("CREATE INDEX IF NOT EXISTS vector_features_geom ON vector_features USING GIST (geom)")


def _m3_ops_indexes():
    with db.connect("ops") as c:
        c.execute("CREATE INDEX IF NOT EXISTS heartbeat_mine_ts ON heartbeat (mine_id, ts)")
        c.execute("CREATE INDEX IF NOT EXISTS batches_ts ON batches (ts)")
    with db.connect("notify") as c:
        c.execute("CREATE INDEX IF NOT EXISTS outbox_ts ON outbox (ts)")
    with db.connect("auth") as c:
        c.execute("CREATE INDEX IF NOT EXISTS audit_ts ON audit_log (ts)")


MIGRATIONS = [
    (1, "persistent stores: auth, ops, notify, scheduler, jobs, monitor", _m1_persistent_stores),
    (2, "PostGIS extension + vector_features", _m2_postgis),
    (3, "indexes for heartbeat, batches, outbox, audit log", _m3_ops_indexes),
]


@contextmanager
def _lock():
    if not db.is_pg():
        yield
        return
    import psycopg
    c = psycopg.connect(db._pg_url(), autocommit=True)
    try:
        c.execute("SELECT pg_advisory_lock(424242)")
        yield
    finally:
        c.execute("SELECT pg_advisory_unlock(424242)")
        c.close()


def applied():
    with db.connect("meta") as c:
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT, note TEXT)")
        return {r["version"]: dict(r) for r in c.execute("SELECT * FROM schema_migrations ORDER BY version")}


def apply(log=print):
    with _lock():
        done = applied()
        for v, name, fn in MIGRATIONS:
            if v in done:
                continue
            note = fn()
            with db.connect("meta") as c:
                c.execute("INSERT INTO schema_migrations VALUES (?, ?, ?, ?)", (v, name, datetime.now().isoformat(timespec="seconds"),
                                                                                str(note) if note else None))
            log(f"migration {v}: {name}" + (f" ({note})" if note else ""))
    return status()


def status():
    done = applied()
    return dict(backend=db.BACKEND, applied=[done[v] for v in sorted(done)],
                pending=[dict(version=v, name=n) for v, n, _ in MIGRATIONS if v not in done])


# ---- SQLite → PostgreSQL ------------------------------------------------------------------------
PERSISTENT = ("auth", "ops", "registry", "notify", "scheduler", "jobs", "monitor")


def import_sqlite(src=DATA, force=False, log=print):
    if not db.is_pg():
        raise SystemExit("set KD_DATABASE_URL to the PostgreSQL target first")
    apply(log)
    src = Path(src)
    report = {}
    for store in db.STORES:
        if store == "meta":
            continue
        f = src / db.path_of(store).name
        if not f.exists():
            continue
        con = sqlite3.connect(f)
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in names:
            df = pd.read_sql(f'SELECT * FROM "{t}"', con)
            if store in PERSISTENT and db.table_exists(t, store):
                with db.connect(store) as c:
                    n = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                if n and not force:
                    log(f"  {store}.{t}: target has {n} rows, kept (use --force to overwrite)")
                    report[f"{store}.{t}"] = "kept"
                    continue
                with db.connect(store) as c:  # keep the migration-created schema (PKs, serials); replace the rows
                    c.execute(f"DELETE FROM {t}")
                db.to_sql(df, t, store, if_exists="append")
                if "id" in df.columns and t in db.SERIAL_TABLES and len(df):
                    with db.connect(store) as c:
                        c.execute(f"SELECT setval(pg_get_serial_sequence('{store}.{t}', 'id'), {int(df.id.max())})")
            else:
                db.to_sql(df, t, store, if_exists="replace")
            report[f"{store}.{t}"] = len(df)
            log(f"  {store}.{t}: {len(df):,} rows")
        con.close()
    # rebuildable geo store: add PostGIS geometry to what was imported
    from . import storage
    if db.table_exists("grid_cells", "geo"):
        storage._spatialize_grid()
    if db.table_exists("vectors", "geo"):
        import json
        with db.connect("geo") as c:
            vecs = [(r["name"], r["geojson"]) for r in c.execute("SELECT name, geojson FROM vectors")]
        for name, gj in vecs:
            storage.write_vector(name, json.loads(gj))
        log(f"  geo: PostGIS geometry for grid_cells + {len(vecs)} vector layers")
    return report


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "status":
        import json
        print(json.dumps(status(), indent=1))
    elif a and a[0] == "import-sqlite":
        rest = [x for x in a[1:] if not x.startswith("--")]
        import_sqlite(rest[0] if rest else DATA, force="--force" in a)
    else:
        apply()
