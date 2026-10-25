"""Monitoring: Prometheus metrics, readiness checks, live forecast accuracy and drift.

/metrics (Prometheus text format, bearer KD_METRICS_TOKEN if set)
  kd_http_requests_total{method,route,status}, kd_http_request_seconds{route}   request middleware
  kd_ingest_records{table}, kd_ingest_dead_letter, kd_ingest_batches{channel,result}
  kd_heartbeat_age_seconds{mine}, kd_fleet_available_ratio{mine}               live telemetry
  kd_outbox_messages{status}, kd_jobs{status}, kd_job_last_success_age_seconds{kind}
  kd_data_age_days                                                             freshness of production data
  kd_model_metric{task,model,metric}, kd_model_champion{task,model}            model registry
  kd_forecast_live_mae_t, kd_forecast_live_coverage, kd_forecast_drift_ratio   live accuracy vs backtest
  kd_feature_psi{feature}                                                      input (data) drift
  kd_backup_age_seconds, kd_restore_drill_ok

Live forecast accuracy: every pipeline run logs the champion's 30-day daily forecast per mine
(issued at the last fully observed day). As actuals arrive, forecasts are scored on days that have
since been observed. Drift ratio = live daily MAE / backtest daily MAE (>1.25 warn, >1.5 alert).
Feature drift: population stability index of each driver, last 30 days vs the preceding year
(>0.1 moderate, >0.25 significant).
"""
import json
import os
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from . import db
from .config import MINES, MODELS

DRIVERS = ["rain_mm", "excavator_avail", "fleet_ratio", "manpower_ratio", "blasts"]


def init():
    with db.connect("monitor") as c:
        c.execute("""CREATE TABLE IF NOT EXISTS forecast_log (issued TEXT, mine_id TEXT, target_date TEXT, horizon INTEGER,
                     p10 REAL, p50 REAL, p90 REAL, model TEXT, version TEXT, logged_at TEXT,
                     PRIMARY KEY (issued, mine_id, target_date))""")
        c.execute("CREATE INDEX IF NOT EXISTS forecast_log_target ON forecast_log (target_date)")


# ---- forecast log + live accuracy --------------------------------------------------------------
def log_forecasts():
    """Persist the serving model's forecast so it can be scored when the actuals arrive."""
    from . import registry, shortfall
    init()
    shortfall.reload()
    champ, _ = registry.champion("shortfall")
    rows = []
    for m in MINES:
        f = shortfall.forecast(m["id"])
        issued = f["data_through"]
        for k, d in enumerate(f["daily"], 1):
            rows.append((issued, m["id"], d["date"], k, d["p10"], d["p50"], d["p90"], shortfall.serving_family(),
                         champ["version"] if champ else None, datetime.now().isoformat(timespec="seconds")))
    with db.connect("monitor") as c:
        c.executemany("INSERT OR REPLACE INTO forecast_log VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return dict(logged=len(rows), issued=rows[0][0] if rows else None)


def live_accuracy():
    from . import storage
    init()
    fl = db.read_sql("SELECT * FROM forecast_log", "monitor")
    out = dict(issues=int(fl.issued.nunique()) if len(fl) else 0, first_issued=fl.issued.min() if len(fl) else None, scored_days=0)
    try:
        bt = json.loads((MODELS / "shortfall_card.json").read_text())["backtest"]
        out["backtest_daily_mae_t"] = bt.get("model_daily_mae_t")
    except (OSError, KeyError, ValueError):
        bt = {}
    if not len(fl):
        out["status"] = "accumulating"
        return out
    act = storage.read_table("production_daily")[["date", "mine_id", "actual_t"]]
    j = fl.merge(act, left_on=["target_date", "mine_id"], right_on=["date", "mine_id"], how="inner")
    out["scored_days"] = int(j.target_date.nunique())
    out["scored_points"] = int(len(j))
    if not len(j):
        out["status"] = "accumulating"
        out["note"] = "Forecasts are logged; they are scored as the forecast days are observed."
        return out
    err = (j.p50 - j.actual_t).abs()
    out["live_daily_mae_t"] = round(float(err.mean()), 1)
    out["live_coverage"] = round(float(((j.actual_t >= j.p10) & (j.actual_t <= j.p90)).mean()), 3)
    out["by_horizon"] = [dict(horizon=str(h), n=int(len(g)), mae_t=round(float((g.p50 - g.actual_t).abs().mean()), 1))
                         for h, g in j.assign(hb=pd.cut(j.horizon, [0, 7, 14, 30], labels=["1-7 d", "8-14 d", "15-30 d"]))
                         .groupby("hb", observed=True)]
    ref = bt.get("model_daily_mae_t")
    if ref:
        out["drift_ratio"] = round(out["live_daily_mae_t"] / ref, 2)
        enough = out["scored_days"] >= 7
        out["status"] = ("ok" if out["drift_ratio"] <= 1.25 else "warn" if out["drift_ratio"] <= 1.5 else "alert") if enough \
            else "accumulating"
    return out


def feature_drift(window=30, ref_days=365, bins=10):
    from . import storage
    h = storage.read_table("production_daily")
    h["date"] = pd.to_datetime(h.date)
    end = h.date.max()
    cur = h[h.date > end - pd.Timedelta(days=window)]
    ref = h[(h.date <= end - pd.Timedelta(days=window)) & (h.date > end - pd.Timedelta(days=window + ref_days))]
    out = []
    for f in DRIVERS:
        a, b = ref[f].dropna().values, cur[f].dropna().values
        if len(a) < 50 or len(b) < 10:
            continue
        edges = np.unique(np.quantile(a, np.linspace(0, 1, bins + 1)))
        if len(edges) < 3:  # near-constant feature (e.g. blasts): compare category shares
            vals = np.union1d(np.unique(a), np.unique(b))
            pa = np.array([(a == v).mean() for v in vals]) + 1e-4
            pb = np.array([(b == v).mean() for v in vals]) + 1e-4
        else:
            edges[0], edges[-1] = -np.inf, np.inf
            pa = np.histogram(a, edges)[0] / len(a) + 1e-4
            pb = np.histogram(b, edges)[0] / len(b) + 1e-4
        psi = float(np.sum((pb - pa) * np.log(pb / pa)))
        out.append(dict(feature=f, psi=round(psi, 3), level="significant" if psi > 0.25 else "moderate" if psi > 0.1 else "stable",
                        ref_mean=round(float(a.mean()), 3), cur_mean=round(float(b.mean()), 3)))
    return dict(window_days=window, reference_days=ref_days, through=str(end.date()), features=out)


# ---- readiness ---------------------------------------------------------------------------------
def readiness():
    """Checks for /api/ready (and the System page)."""
    from . import ingest, storage
    checks = []
    ok, detail = db.ping()
    checks.append(dict(name="database", ok=ok, detail=detail + (" + PostGIS" if ok and db.has_postgis() else "")))
    if ok:
        has = db.table_exists("grid_cells", "geo") and db.table_exists("production_daily", "warehouse")
        checks.append(dict(name="warehouse built", ok=has, detail="grid + integrated tables present" if has else "run the pipeline"))
        if has:
            last = storage.read_table("production_daily", where="ORDER BY date DESC LIMIT 1").date.iloc[0]
            age = (date.today() - date.fromisoformat(last)).days
            # stale data is a warning (alerts + Grafana), not a reason to take the API out of rotation
            checks.append(dict(name="data freshness", ok=age <= 3, required=False, age_days=age,
                               detail=f"production data through {last} ({age} d old)" + ("" if age <= 3 else ": run the pipeline")))
    models = [(MODELS / "shortfall.joblib").exists(), (MODELS / "reserve_card.json").exists()]
    checks.append(dict(name="models", ok=all(models), detail="serving models present" if all(models) else "models missing"))
    if ok:
        try:
            hb = ingest.live_equipment(minutes=15)
            checks.append(dict(name="telemetry", ok=True, required=False,
                               detail=f"{len(hb)} machines reported in the last 15 min" if hb else "no live heartbeats (optional)"))
        except Exception as e:  # noqa: BLE001
            checks.append(dict(name="telemetry", ok=False, required=False, detail=str(e)[:120]))
    ready = all(c["ok"] for c in checks if c.get("required", True))
    return dict(ready=ready, checks=checks, checked_at=datetime.now().isoformat(timespec="seconds"))


# ---- Prometheus ------------------------------------------------------------------------------
_prom = {}


def _registry():
    if "reg" not in _prom:
        from prometheus_client import CollectorRegistry, Counter, Histogram
        reg = CollectorRegistry()
        _prom["reg"] = reg
        _prom["req"] = Counter("kd_http_requests_total", "HTTP requests", ["method", "route", "status"], registry=reg)
        _prom["lat"] = Histogram("kd_http_request_seconds", "HTTP request latency", ["route"], registry=reg,
                                 buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))
        reg.register(_Collector())
    return _prom["reg"]


def observe_request(method, route, status, seconds):
    _registry()
    _prom["req"].labels(method, route, str(status)).inc()
    _prom["lat"].labels(route).observe(seconds)


_slow_cache = dict(t=0, val=None)


def _slow_metrics():
    """Accuracy/drift are heavier; recompute at most every 5 minutes."""
    if time.time() - _slow_cache["t"] > 300:
        try:
            _slow_cache["val"] = (live_accuracy(), feature_drift())
        except Exception:  # noqa: BLE001
            _slow_cache["val"] = None
        _slow_cache["t"] = time.time()
    return _slow_cache["val"]


class _Collector:
    def collect(self):
        from prometheus_client.core import GaugeMetricFamily
        from . import backup, ingest, jobs, notify

        def g(name, doc, labels=()):
            return GaugeMetricFamily(name, doc, labels=list(labels))

        try:
            r = readiness()
            m = g("kd_database_up", "1 when the configured database is reachable")
            db_check = next((x for x in r["checks"] if x["name"] == "database"), {})
            m.add_metric([], 1.0 if db_check.get("ok") else 0.0)
            yield m
            m = g("kd_ready", "1 when all required readiness checks pass")
            m.add_metric([], 1.0 if r["ready"] else 0.0)
            yield m
        except Exception:  # noqa: BLE001
            pass

        try:
            st = ingest.status()
            m = g("kd_ingest_records", "Records in the operational store", ["table"])
            for t, n in st["counts"].items():
                m.add_metric([t], n)
            yield m
            m = g("kd_ingest_dead_letter", "Dead-lettered records")
            m.add_metric([], st["counts"].get("dead_letter", 0))
            yield m
            m = g("kd_ingest_batches", "Ingestion batch records by channel", ["channel", "result"])
            for ch in st["by_channel"]:
                m.add_metric([ch["channel"], "ok"], ch["ok"] or 0)
                m.add_metric([ch["channel"], "rejected"], ch["bad"] or 0)
            yield m
            hb = ingest.live_equipment(minutes=60 * 24)
            age = g("kd_heartbeat_age_seconds", "Seconds since the mine's last equipment heartbeat", ["mine"])
            avail = g("kd_fleet_available_ratio", "Share of reporting machines working or idle", ["mine"])
            now = datetime.now()
            for mid in {r["mine_id"] for r in hb}:
                rs = [r for r in hb if r["mine_id"] == mid]
                age.add_metric([mid], (now - datetime.fromisoformat(max(r["ts"] for r in rs))).total_seconds())
                avail.add_metric([mid], sum(r["status"] in ("working", "idle") for r in rs) / len(rs))
            yield age
            yield avail
        except Exception:  # noqa: BLE001 — a store being rebuilt must not break the scrape
            pass
        try:
            m = g("kd_outbox_messages", "Notification outbox by status", ["status"])
            for k, v in notify.outbox(1)["counts"].items():
                m.add_metric([k], v)
            yield m
            m = g("kd_jobs", "Jobs by status", ["status"])
            for k, v in jobs.counts().items():
                m.add_metric([k], v)
            yield m
            m = g("kd_job_last_success_age_seconds", "Seconds since the last successful job of each kind", ["kind"])
            for kind in jobs.KINDS:
                j = jobs.latest(kind)
                if j and j["status"] == "done" and j["finished"]:
                    m.add_metric([kind], (datetime.now() - datetime.fromisoformat(j["finished"])).total_seconds())
            yield m
        except Exception:  # noqa: BLE001
            pass
        try:
            from . import storage
            last = storage.read_table("production_daily", where="ORDER BY date DESC LIMIT 1").date.iloc[0]
            m = g("kd_data_age_days", "Days since the last fully observed production day")
            m.add_metric([], (date.today() - date.fromisoformat(last)).days)
            yield m
        except Exception:  # noqa: BLE001
            pass
        try:
            mm = g("kd_model_metric", "Latest training-run metrics per model", ["task", "model", "metric"])
            ch = g("kd_model_champion", "1 for the serving model", ["task", "model"])
            for task, keys in (("reserve", ("gen_score", "cv_auc", "occ_auc_far")), ("shortfall", ("mape_30d", "daily_mae_t"))):
                card = json.loads((MODELS / f"{task}_card.json").read_text())
                for b in card.get("leaderboard", []):
                    for k in keys:
                        if b.get(k) is not None:
                            mm.add_metric([task, b["name"], k], float(b[k]))
                    if b.get("version"):
                        ch.add_metric([task, b["name"]], 1.0 if b.get("champion") else 0.0)
            yield mm
            yield ch
        except Exception:  # noqa: BLE001
            pass
        slow = _slow_metrics()
        if slow:
            acc, drift = slow
            if acc.get("live_daily_mae_t") is not None:
                for name, key, doc in (("kd_forecast_live_mae_t", "live_daily_mae_t", "Live daily MAE of logged forecasts (t)"),
                                       ("kd_forecast_live_coverage", "live_coverage", "Live P10-P90 coverage"),
                                       ("kd_forecast_drift_ratio", "drift_ratio", "Live MAE / backtest MAE")):
                    if acc.get(key) is not None:
                        m = g(name, doc)
                        m.add_metric([], acc[key])
                        yield m
            m = g("kd_forecast_scored_days", "Days with logged forecasts that have been observed")
            m.add_metric([], acc.get("scored_days", 0))
            yield m
            m = g("kd_feature_psi", "Population stability index, last 30 d vs previous year", ["feature"])
            for f in drift["features"]:
                m.add_metric([f["feature"]], f["psi"])
            yield m
        try:
            b = backup.status()
            if b.get("latest"):
                m = g("kd_backup_age_seconds", "Seconds since the latest backup")
                m.add_metric([], (datetime.now() - datetime.fromisoformat(b["latest"]["created"])).total_seconds())
                yield m
            if b.get("last_drill"):
                m = g("kd_restore_drill_ok", "1 if the latest restore drill passed")
                m.add_metric([], 1.0 if b["last_drill"]["ok"] else 0.0)
                yield m
        except Exception:  # noqa: BLE001
            pass


def metrics_text():
    from prometheus_client import generate_latest
    return generate_latest(_registry())


def snapshot():
    from . import jobs
    acc, drift = _slow_metrics() or (live_accuracy(), feature_drift())
    return dict(readiness=readiness(), accuracy=acc, feature_drift=drift, database=db.describe(),
                jobs=dict(counts=jobs.counts(), recent=jobs.recent(8), mode="external" if jobs.external() else "in-process"),
                grafana_url=os.environ.get("KD_GRAFANA_URL"))


def refresh():
    _slow_cache["t"] = 0
