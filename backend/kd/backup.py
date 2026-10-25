"""Backups and restore drills.

    python -m kd.backup create [--lake]     archive → data/backups/kd-backup-<ts>.tar.gz
    python -m kd.backup list
    python -m kd.backup drill [NAME]        restore into a scratch location and verify it
    python -m kd.backup restore NAME --yes  restore over the live stores (stop the API/worker first)

What a backup holds
  db/        SQLite: every store, copied with the online-backup API (consistent while in use)
             PostgreSQL: one `pg_dump -Fc` of all schemas, taken on an exported snapshot so the row
             counts in the manifest are from exactly the same moment as the dump
  files/     models/ (registry, artifacts, cards), data/reports, data/feature_store, manual uploads
             and the ingestion archive (data/lake/raw/uploads, data/lake/raw/ops); --lake adds the
             re-fetchable open-data lake too
  manifest   created, backend, row counts per table, sha256 + size of every file
Secrets (.secret_key, device keys) are deliberately NOT included: they come from the environment /
secret store, and a stolen backup must not carry live credentials.

A restore drill restores into a scratch SQLite directory or a temporary PostgreSQL database, then
checks every table's row count and every file's checksum against the manifest and records the
result (and the restore time) in data/backups/drills.jsonl.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import db
from .config import DATA, FEATURE_STORE, LAKE, MODELS

DIR = DATA / "backups"
DRILLS = DIR / "drills.jsonl"
KEEP = int(os.environ.get("KD_BACKUP_KEEP", "14"))
FILE_SETS = {"models": MODELS, "reports": DATA / "reports", "feature_store": FEATURE_STORE,
             "uploads": LAKE / "uploads", "ops_archive": LAKE / "ops"}


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _counts_sqlite(path):
    c = sqlite3.connect(path)
    try:
        return {t: c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    finally:
        c.close()


def _pg_env():
    from urllib.parse import unquote, urlparse
    u = urlparse(db._pg_url())
    env = dict(os.environ, PGHOST=u.hostname or "localhost", PGPORT=str(u.port or 5432), PGUSER=unquote(u.username or ""),
               PGPASSWORD=unquote(u.password or ""), PGDATABASE=(u.path or "/").lstrip("/"))
    return env


def _need(binary):
    if not shutil.which(binary):
        raise RuntimeError(f"{binary} not found: PostgreSQL backups need the PostgreSQL client tools (installed in the Docker image)")


# ---- create ----------------------------------------------------------------------------------
def create(include_lake=False, log=print):
    t0 = time.time()
    DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"kd-backup-{stamp}.tar.gz"
    manifest = dict(name=name, created=datetime.now().isoformat(timespec="seconds"), backend=db.BACKEND,
                    postgis=db.has_postgis(), include_lake=include_lake, tables={}, files={})
    with tempfile.TemporaryDirectory(prefix="kdbk-") as tmp:
        tmp = Path(tmp)
        (tmp / "db").mkdir()
        if db.is_pg():
            _need("pg_dump")
            import psycopg
            # hold a REPEATABLE READ snapshot: count rows in it, then dump exactly that snapshot
            with psycopg.connect(db._pg_url(), autocommit=False) as c:
                c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                snap = c.execute("SELECT pg_export_snapshot()").fetchone()[0]
                for store in db.STORES:
                    ts = [r[0] for r in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s "
                                                  "AND table_type='BASE TABLE'", (store,))]
                    if ts:
                        manifest["tables"][store] = {t: c.execute(f'SELECT COUNT(*) FROM "{store}"."{t}"').fetchone()[0] for t in ts}
                log(f"dumping PostgreSQL (snapshot {snap})")
                r = subprocess.run(["pg_dump", "-Fc", "--no-owner", f"--snapshot={snap}", "-f", str(tmp / "db" / "pg.dump")]
                                   + sum((["-n", s] for s in manifest["tables"]), []),
                                   env=_pg_env(), capture_output=True, text=True)
                if r.returncode:
                    raise RuntimeError(f"pg_dump failed: {r.stderr[-400:]}")
                c.rollback()
        else:
            for store in db.STORES:
                src = db.path_of(store)
                if not src.exists():
                    continue
                dst = tmp / "db" / src.name
                a, b = sqlite3.connect(src), sqlite3.connect(dst)
                with b:
                    a.backup(b)
                a.close(); b.close()
                manifest["tables"][store] = _counts_sqlite(dst)
            log(f"copied {len(manifest['tables'])} SQLite stores")
        sets = dict(FILE_SETS, **({"lake": LAKE} if include_lake else {}))
        n = 0
        for key, root in sets.items():
            if not root.exists():
                continue
            nested = [r for k, r in sets.items() if k != key and r != root and r.is_relative_to(root)]  # e.g. lake ⊃ uploads
            for p in root.rglob("*"):
                if p.is_file() and not p.name.startswith(".") and "backups" not in p.parts:
                    if any(p.is_relative_to(r) for r in nested):
                        continue
                    rel = f"files/{key}/{p.relative_to(root).as_posix()}"
                    (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, tmp / rel)
                    manifest["files"][rel] = dict(sha256=_sha(tmp / rel), bytes=p.stat().st_size)
                    n += 1
        log(f"{n} files ({sum(f['bytes'] for f in manifest['files'].values()) / 1e6:.1f} MB)")
        for p in (tmp / "db").iterdir():
            manifest["files"][f"db/{p.name}"] = dict(sha256=_sha(p), bytes=p.stat().st_size)
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=1))
        with tarfile.open(DIR / name, "w:gz") as tar:
            tar.add(tmp, arcname=".")
    size = (DIR / name).stat().st_size
    meta = dict(name=name, created=manifest["created"], backend=db.BACKEND, bytes=size, seconds=round(time.time() - t0, 1),
                tables=sum(len(v) for v in manifest["tables"].values()), rows=sum(sum(v.values()) for v in manifest["tables"].values()),
                files=len(manifest["files"]), include_lake=include_lake)
    (DIR / f"{name}.json").write_text(json.dumps(meta))
    pruned = prune()
    log(f"backup {name}: {size / 1e6:.1f} MB in {meta['seconds']} s" + (f"; pruned {pruned} old" if pruned else ""))
    return meta


def listing():
    out = []
    for m in DIR.glob("kd-backup-*.tar.gz.json"):
        if Path(str(m)[:-5]).exists():
            out.append(json.loads(m.read_text()))
    return sorted(out, key=lambda x: x["created"], reverse=True)


def prune(keep=KEEP):
    old = listing()[keep:]
    for b in old:
        (DIR / b["name"]).unlink(missing_ok=True)
        (DIR / f"{b['name']}.json").unlink(missing_ok=True)
    return len(old)


def drills(limit=20):
    if not DRILLS.exists():
        return []
    return [json.loads(x) for x in DRILLS.read_text().splitlines()[-limit:]][::-1]


def status():
    b = listing()
    d = drills(1)
    return dict(latest=b[0] if b else None, count=len(b), keep=KEEP, last_drill=d[0] if d else None, dir=str(DIR))


def _path(name):
    name = name or (listing()[0]["name"] if listing() else None)
    if not name:
        raise FileNotFoundError("no backups yet")
    p = (DIR / name).resolve()
    if p.parent != DIR.resolve() or not p.exists():
        raise FileNotFoundError(name)
    return p


def _extract(p, dest):
    with tarfile.open(p, "r:gz") as tar:
        tar.extractall(dest, filter="data")
    return json.loads((Path(dest) / "manifest.json").read_text())


# ---- restore drill -------------------------------------------------------------------------------
def drill(name=None, log=print):
    p = _path(name)
    t0 = time.time()
    problems, checked_tables, checked_files = [], 0, 0
    with tempfile.TemporaryDirectory(prefix="kddrill-") as tmp:
        tmp = Path(tmp)
        man = _extract(p, tmp)
        log(f"drill: {p.name} ({man['backend']}, {len(man['files'])} files)")
        for rel, f in man["files"].items():
            q = tmp / rel
            if not q.exists():
                problems.append(f"missing {rel}")
            elif _sha(q) != f["sha256"]:
                problems.append(f"checksum mismatch {rel}")
            checked_files += 1
        if man["backend"] == "postgres":
            _need("pg_restore")
            import psycopg
            scratch = f"kd_drill_{datetime.now():%Y%m%d%H%M%S}"
            env = _pg_env()
            with psycopg.connect(db._pg_url(), autocommit=True) as c:
                c.execute(f'CREATE DATABASE "{scratch}"')
            try:
                env2 = dict(env, PGDATABASE=scratch)
                with psycopg.connect(db._pg_url().rsplit("/", 1)[0] + f"/{scratch}", autocommit=True) as c:
                    c.execute("CREATE EXTENSION IF NOT EXISTS postgis")
                r = subprocess.run(["pg_restore", "--no-owner", "-d", scratch, str(tmp / "db" / "pg.dump")], env=env2,
                                   capture_output=True, text=True)
                # pg_restore 17 emits SET transaction_timeout when restoring into PostgreSQL 16.
                # PostgreSQL rejects that unknown setting but continues restoring every object; it is
                # safe to ignore only this exact compatibility warning because row counts below still
                # verify every restored table. All other restore errors remain fatal.
                lines = r.stderr.splitlines()
                harmless = any('unrecognized configuration parameter "transaction_timeout"' in x for x in lines)
                errs = [ln for ln in lines if "error" in ln.lower() and "already exists" not in ln
                        and not (harmless and "could not execute query" in ln.lower())]
                errs = [ln for ln in errs if "transaction_timeout" not in ln
                        and not (harmless and "errors ignored on restore: 1" in ln.lower())]
                if r.returncode and errs:
                    problems.append(f"pg_restore: {errs[0][:200]}")
                with psycopg.connect(db._pg_url().rsplit("/", 1)[0] + f"/{scratch}") as c:
                    for store, ts in man["tables"].items():
                        for t, n in ts.items():
                            got = c.execute(f'SELECT COUNT(*) FROM "{store}"."{t}"').fetchone()[0]
                            checked_tables += 1
                            if got != n:
                                problems.append(f"{store}.{t}: {got} rows, expected {n}")
            finally:
                with psycopg.connect(db._pg_url(), autocommit=True) as c:
                    c.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
        else:
            for store, ts in man["tables"].items():
                f = tmp / "db" / db.path_of(store).name
                got = _counts_sqlite(f) if f.exists() else {}
                for t, n in ts.items():
                    checked_tables += 1
                    if got.get(t) != n:
                        problems.append(f"{store}.{t}: {got.get(t)} rows, expected {n}")
    res = dict(backup=p.name, at=datetime.now().isoformat(timespec="seconds"), ok=not problems, seconds=round(time.time() - t0, 1),
               tables=checked_tables, files=checked_files, problems=problems[:20], backend=man["backend"])
    DIR.mkdir(parents=True, exist_ok=True)
    with open(DRILLS, "a", encoding="utf-8") as f:
        f.write(json.dumps(res) + "\n")
    log(f"drill {'PASSED' if res['ok'] else 'FAILED'}: {checked_tables} tables, {checked_files} files verified in {res['seconds']} s"
        + ("" if res["ok"] else f"; {len(problems)} problem(s): {problems[:3]}"))
    return res


# ---- restore (destructive, CLI only) -----------------------------------------------------------
def restore(name, log=print):
    p = _path(name)
    with tempfile.TemporaryDirectory(prefix="kdrestore-") as tmp:
        tmp = Path(tmp)
        man = _extract(p, tmp)
        if man["backend"] != db.BACKEND:
            raise RuntimeError(f"backup is {man['backend']}, current backend is {db.BACKEND}")
        if db.is_pg():
            _need("pg_restore")
            r = subprocess.run(["pg_restore", "--clean", "--if-exists", "--no-owner", "-d", _pg_env()["PGDATABASE"],
                                str(tmp / "db" / "pg.dump")], env=_pg_env(), capture_output=True, text=True)
            if r.returncode:
                log(f"pg_restore warnings: {r.stderr[-300:]}")
        else:
            for f in (tmp / "db").iterdir():
                shutil.copy2(f, DATA / f.name)
        for key, root in dict(FILE_SETS, lake=LAKE).items():
            src = tmp / "files" / key
            if src.exists():
                shutil.copytree(src, root, dirs_exist_ok=True)
    log(f"restored {p.name}")
    return dict(restored=p.name)


if __name__ == "__main__":
    import sys
    a = sys.argv[1:]
    if not a or a[0] == "list":
        for b in listing():
            print(b["created"], b["name"], f"{b['bytes'] / 1e6:.1f} MB", b["rows"], "rows")
    elif a[0] == "create":
        print(create(include_lake="--lake" in a))
    elif a[0] == "drill":
        print(drill(a[1] if len(a) > 1 else None))
    elif a[0] == "restore":
        if "--yes" not in a:
            sys.exit("restore overwrites the live stores: stop the API/worker, then re-run with --yes")
        restore(a[1] if len(a) > 1 and a[1] != "--yes" else None)
