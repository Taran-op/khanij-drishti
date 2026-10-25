"""Database layer: SQLite (default, zero-setup) or PostgreSQL + PostGIS (KD_DATABASE_URL).

Logical stores and where they live:

    store       SQLite file               PostgreSQL schema   rebuilt by pipeline?
    warehouse   data/warehouse.sqlite     warehouse           yes (processed tables, logs)
    geo         data/geodb.sqlite         geo                 yes (grid cells + PostGIS geometry, vectors)
    auth        data/auth.sqlite          auth                no  (users, audit log)
    ops         data/ops.sqlite           ops                 no  (ingestion store, devices, heartbeats)
    registry    data/registry.sqlite      registry            no  (manual uploads)
    notify      data/notify.sqlite        notify              no  (delivery settings, recipients, outbox)
    scheduler   data/scheduler.sqlite     scheduler           no  (schedules, run history)
    jobs        data/jobs.sqlite          jobs                no  (pipeline job queue for the worker)
    monitor     data/monitor.sqlite       monitor             no  (forecast log, drift history)
    meta        data/meta.sqlite          meta                no  (schema_migrations)

Application code writes portable SQL with `?` placeholders (or `:name` with dict params) and the
SQLite idioms `INSERT OR REPLACE` / `INSERT OR IGNORE`; on PostgreSQL they are translated
(%s placeholders, ON CONFLICT upserts using the table's primary key, BIGSERIAL ids, RETURNING id
for lastrowid). Rows support both row["col"] and row[0].

    KD_DATABASE_URL=postgresql://kd:secret@localhost:5432/khanij
"""
import os
import re
import sqlite3
import threading
from pathlib import Path

from .config import DATA

URL = os.environ.get("KD_DATABASE_URL", "").strip()
BACKEND = "postgres" if URL.startswith(("postgres://", "postgresql://", "postgresql+psycopg://")) else "sqlite"
STORES = ("warehouse", "geo", "auth", "ops", "registry", "notify", "scheduler", "jobs", "monitor", "meta")
_FILES = {"geo": "geodb.sqlite"}
SERIAL_TABLES = {"recipients", "outbox", "schedules", "runs", "jobs"}  # INTEGER PRIMARY KEY AUTOINCREMENT id


def path_of(store):
    return DATA / _FILES.get(store, f"{store}.sqlite")


def store_of(db):
    """Accepts a store name or one of the legacy Path constants (WAREHOUSE, GEODB, REGISTRY…)."""
    if isinstance(db, str) and db in STORES:
        return db
    name = Path(db).name
    for s in STORES:
        if path_of(s).name == name:
            return s
    raise KeyError(f"unknown store {db}")


def is_pg():
    return BACKEND == "postgres"


def _pg_url():
    u = URL
    for p in ("postgresql+psycopg://", "postgres://"):
        if u.startswith(p):
            u = "postgresql://" + u[len(p):]
    return u


# ---- rows ------------------------------------------------------------------------------------
class Row(dict):
    """dict that also answers row[0] like sqlite3.Row."""
    def __getitem__(self, k):
        if isinstance(k, int):
            return list(self.values())[k]
        return super().__getitem__(k)


def _pg_row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Row(zip(names, values))


# ---- SQL translation -------------------------------------------------------------------------
_meta_cache = {}
_meta_lock = threading.Lock()


def _table_info(conn, schema, table):
    key = (schema, table)
    with _meta_lock:
        if key in _meta_cache:
            return _meta_cache[key]
    cols = [r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
        (schema, table)).fetchall()]
    pk = [r[0] for r in conn.execute(
        """SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)
           WHERE i.indrelid=%s::regclass AND i.indisprimary""", (f'"{schema}"."{table}"',)).fetchall()]
    with _meta_lock:
        _meta_cache[key] = (cols, pk)
    return cols, pk


_INSERT_RE = re.compile(r"^\s*INSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO\s+(\w+)\s*(\(([^)]*)\))?\s*VALUES\s*(.*)$", re.I | re.S)


def _translate(sql, params, conn, schema):
    s = sql.strip()
    if re.match(r"^PRAGMA\b", s, re.I):
        return None, False
    # DDL
    s = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "BIGSERIAL PRIMARY KEY", s, flags=re.I)
    s = re.sub(r"\bREAL\b", "DOUBLE PRECISION", s)
    returning = False
    m = _INSERT_RE.match(s)
    if m:
        mode, table, _, cols, values = m.groups()
        tcols, pk = _table_info(conn, schema, table)
        cols = [c.strip() for c in cols.split(",")] if cols else tcols
        head = f"INSERT INTO {table} ({', '.join(cols)}) VALUES {values}"
        if mode.upper() == "IGNORE" or not pk:
            s = head + " ON CONFLICT DO NOTHING"
        else:
            upd = [c for c in cols if c not in pk]
            s = head + f" ON CONFLICT ({', '.join(pk)}) DO " + (
                "UPDATE SET " + ", ".join(f"{c}=EXCLUDED.{c}" for c in upd) if upd else "NOTHING")
    elif re.match(r"^INSERT\s+INTO\s+(\w+)", s, re.I) and re.match(r"^INSERT\s+INTO\s+(\w+)", s, re.I).group(1) in SERIAL_TABLES \
            and "RETURNING" not in s.upper():
        s += " RETURNING id"
        returning = True
    # placeholders (a literal % only needs escaping when parameters are bound)
    if not params:
        return s, returning
    s = s.replace("%", "%%")
    if isinstance(params, dict) or (isinstance(params, (list, tuple)) and params and isinstance(params[0], dict)):
        s = re.sub(r"(?<![:\w]):([A-Za-z_]\w*)", r"%(\1)s", s)
    else:
        s = s.replace("?", "%s")
    return s, returning


class Cursor:
    def __init__(self, cur, lastrowid=None):
        self._c, self.lastrowid = cur, lastrowid

    @property
    def rowcount(self):
        return self._c.rowcount

    def fetchone(self):
        return self._c.fetchone()

    def fetchall(self):
        return self._c.fetchall()

    def __iter__(self):
        return iter(self._c.fetchall() if self._c.description else [])


class PgConn:
    """sqlite3.Connection-shaped wrapper over psycopg, scoped to one schema."""
    def __init__(self, store):
        import psycopg
        self.schema = store
        self._c = psycopg.connect(_pg_url(), row_factory=_pg_row_factory, autocommit=False,
                                  options=f"-c search_path={store},public")

    def execute(self, sql, params=()):
        s, returning = _translate(sql, params, self._c, self.schema)
        if s is None:
            return Cursor(self._c.cursor())
        cur = self._c.execute(s, params if params else None)
        lastrowid = None
        if returning:
            r = cur.fetchone()
            lastrowid = r[0] if r else None
        return Cursor(cur, lastrowid)

    def executemany(self, sql, seq):
        seq = list(seq)
        if not seq:
            return Cursor(self._c.cursor())
        s, _ = _translate(sql, seq, self._c, self.schema)
        if s is None:
            return Cursor(self._c.cursor())
        cur = self._c.cursor()
        cur.executemany(s, seq)
        return Cursor(cur)

    def executescript(self, script):
        for stmt in [x.strip() for x in script.split(";") if x.strip()]:
            self.execute(stmt)

    def commit(self):
        self._c.commit()

    def close(self):
        self._c.close()

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        try:
            self._c.rollback() if et else self._c.commit()
        finally:
            self._c.close()


class SqliteConn:
    """sqlite3 connection that closes on exit (sqlite3's own context manager only commits)."""
    def __init__(self, store, timeout=30):
        p = path_of(store)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._c = sqlite3.connect(p, timeout=timeout)
        self._c.row_factory = sqlite3.Row
        if store in ("ops", "jobs", "notify", "scheduler", "monitor"):
            self._c.execute("PRAGMA journal_mode=WAL")

    def __getattr__(self, k):
        return getattr(self._c, k)

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        try:
            self._c.rollback() if et else self._c.commit()
        finally:
            self._c.close()


_schemas_ready = set()


def connect(store):
    store = store_of(store)
    if is_pg():
        if store not in _schemas_ready:
            import psycopg
            with psycopg.connect(_pg_url(), autocommit=True) as c:
                c.execute(f'CREATE SCHEMA IF NOT EXISTS "{store}"')
            _schemas_ready.add(store)
        return PgConn(store)
    return SqliteConn(store)


# ---- pandas ----------------------------------------------------------------------------------
_engines = {}


def engine(store):
    """SQLAlchemy engine for pandas (PostgreSQL only)."""
    from sqlalchemy import create_engine
    store = store_of(store)
    if store not in _engines:
        connect(store).close()  # ensures the schema exists
        _engines[store] = create_engine(_pg_url().replace("postgresql://", "postgresql+psycopg://", 1),
                                        connect_args={"options": f"-c search_path={store},public"}, pool_pre_ping=True)
    return _engines[store]


def read_sql(sql, store, params=None):
    import pandas as pd
    if is_pg():
        from sqlalchemy import text
        with engine(store).connect() as c:
            return pd.read_sql(text(sql), c, params=params)
    with SqliteConn(store) as c:
        return pd.read_sql(sql, c._c, params=params)


def to_sql(df, name, store, if_exists="replace"):
    if is_pg():
        store = store_of(store)
        with engine(store).begin() as c:
            if if_exists == "replace":
                from sqlalchemy import text
                c.execute(text(f'DROP TABLE IF EXISTS "{store}"."{name}" CASCADE'))
                if_exists = "append"
            df.to_sql(name, c, schema=store, if_exists=if_exists, index=False, chunksize=2000, method="multi")
        with _meta_lock:
            _meta_cache.pop((store, name), None)
    else:
        with SqliteConn(store) as c:
            df.to_sql(name, c._c, if_exists=if_exists, index=False)


def table_exists(name, store):
    store = store_of(store)
    if is_pg():
        with connect(store) as c:
            return c.execute("SELECT to_regclass(?) IS NOT NULL", (f'"{store}"."{name}"',)).fetchone()[0]
    if not path_of(store).exists():
        return False
    with SqliteConn(store) as c:
        return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def tables(store):
    store = store_of(store)
    if is_pg():
        with connect(store) as c:
            return [r[0] for r in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=? ORDER BY 1", (store,))]
    if not path_of(store).exists():
        return []
    with SqliteConn(store) as c:
        return [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1")]


def size_bytes(store):
    store = store_of(store)
    if is_pg():
        with connect(store) as c:
            r = c.execute("""SELECT COALESCE(SUM(pg_total_relation_size(format('%I.%I', table_schema, table_name))), 0)
                             FROM information_schema.tables WHERE table_schema=?""", (store,)).fetchone()
            return int(r[0])
    p = path_of(store)
    return p.stat().st_size if p.exists() else 0


def ping():
    """Readiness: can we reach the database? Returns (ok, detail)."""
    try:
        with connect("meta") as c:
            c.execute("SELECT 1").fetchone()
        return True, ("postgresql" if is_pg() else "sqlite")
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"[:200]


def has_postgis():
    if not is_pg():
        return False
    try:
        with connect("geo") as c:
            return c.execute("SELECT 1 FROM pg_extension WHERE extname='postgis'").fetchone() is not None
    except Exception:  # noqa: BLE001
        return False


def describe():
    return dict(backend=BACKEND, postgis=has_postgis(),
                url=re.sub(r"//([^:@/]+):[^@]*@", r"//\1:***@", URL) if is_pg() else str(DATA))
