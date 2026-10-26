"""Layer 5 · Application API (FastAPI). Serves the dashboard, GIS viewer, alerts and reports.

Every route except /api/health and /api/auth/login|demo requires a signed-in user; each route
declares the permission it needs (see kd/auth.py for the role -> permission matrix).

    uvicorn kd.api:app --port 8000
"""
import io
import json
import os
import time
import threading
from datetime import datetime

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (alerts, auth, backup, db, geo, ingest, jobs, migrate, moil_public, monitoring, notify, optimize, pipeline,
               registry, reports, reserve, scheduler, shortfall, storage, terrain, uploads)
from .auth import current_user, require
from .config import AS_OF, BOUNDS, GEODB, HORIZON_DAYS, MINE_BY_ID, MINES, MODELS, RES, ROOT, WAREHOUSE

app = FastAPI(title="KhanijDrishti API", version="1.0")
# The production UI is same-origin behind nginx. Extra browser origins must be explicit; an empty
# default is deliberately safer than accepting every origin.
_origins = [o.strip() for o in os.environ.get("KD_ALLOWED_ORIGINS", os.environ.get("KD_CORS_ORIGINS", "")).split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                   allow_headers=["Authorization", "Content-Type", "X-Device-Key"], allow_credentials=True)

_grid = {}
_sync = dict(t=0.0, seen=None)


def _sync_caches():
    """Work may finish in another process (worker container, another replica): when a newer job has
    finished, drop the in-memory caches so this replica serves the new data/models."""
    now = time.time()
    if now - _sync["t"] < 5:
        return
    _sync["t"] = now
    try:
        last = jobs.last_finished_id()
    except Exception:  # noqa: BLE001 — database briefly unavailable
        return
    if _sync["seen"] is not None and last != _sync["seen"]:
        _grid.clear(); shortfall.reload(); reserve._cache.clear(); terrain.clear(); monitoring.refresh()
    _sync["seen"] = last


SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
                    "Permissions-Policy": "camera=(), microphone=(), geolocation=()"}


@app.middleware("http")
async def _observe(request: Request, call_next):
    t = time.time()
    if request.url.path.startswith("/api/"):
        await run_in_threadpool(_sync_caches)
    response = await call_next(request)
    route = request.scope.get("route")
    path = getattr(route, "path", None) or ("/assets" if request.url.path.startswith("/assets") else "other")
    if path != "/metrics":
        monitoring.observe_request(request.method, path, response.status_code, time.time() - t)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


def _job_view(j=None):
    """The UI's job shape, from the persistent job queue."""
    j = j if j is not None else jobs.latest()
    if not j:
        return dict(running=False, log=[], started=None, finished=None, error=None, kind=None)
    return dict(id=j["id"], kind=j["kind"], status=j["status"], running=j["status"] in ("queued", "running"),
                log=(["waiting for a worker…"] if j["status"] == "queued" else []) + j["log_lines"],
                started=j["started"] or j["created"], finished=j["finished"], error=j["error"], requested_by=j["requested_by"])


def _ready():
    from . import db as _db
    try:
        return _db.table_exists("grid_cells", "geo") and _db.table_exists("production_daily", "warehouse") \
            and (MODELS / "shortfall.joblib").exists()
    except Exception:  # noqa: BLE001 — database not reachable yet
        return False


def _load_grid():
    if "df" not in _grid:
        _grid["df"] = storage.read_table("grid_cells", GEODB)
        _grid["cat"] = storage.read_table("layer_catalog", GEODB)
    return _grid["df"], _grid["cat"]


@app.on_event("startup")
def _startup():
    migrate.apply()  # idempotent; serialised across replicas on PostgreSQL
    if not jobs.external():
        jobs.start_inprocess_worker()
    if not _ready():
        jobs.enqueue("pipeline", by="bootstrap")  # first start: build the warehouse in the background
    if os.environ.get("KD_LIVE_FEED") == "1":
        threading.Timer(5, _feed_start).start()  # once the server is accepting connections
    if os.environ.get("KD_SCHEDULER", "1") == "1":
        scheduler.start()


def _card(name):
    return json.loads((MODELS / f"{name}_card.json").read_text())


# ---- auth ------------------------------------------------------------------------------------
class Login(BaseModel):
    username: str
    password: str


class Registration(Login):
    name: str


def _ip(request):
    return request.client.host if request.client else "?"


@app.post("/api/auth/login")
def login(body: Login, request: Request):
    return auth.login(body.username, body.password, _ip(request))


@app.post("/api/auth/register", status_code=201)
def register(body: Registration, request: Request):
    return auth.register(body.username, body.name, body.password, _ip(request))


@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return user


@app.post("/api/auth/logout")
def logout(request: Request, user=Depends(current_user)):
    auth.logout(user, _ip(request))
    return dict(ok=True)


@app.get("/api/auth/demo")
def demo_accounts():
    return auth.demo_accounts()


@app.get("/api/auth/users")
def users(user=Depends(require("users"))):
    return auth.users_and_audit()


def _visible_alerts(user):
    """Alerts are addressed to audiences; each role sees its own (admin sees all)."""
    al = storage.read_table("alerts")
    aud = auth.alert_audience(user)
    if aud is None:
        return al
    return al[al.audience.str.split(",").map(lambda a: bool(aud & set(a)))]


# ---- overview --------------------------------------------------------------------------------
@app.get("/ready", include_in_schema=False)
@app.get("/api/ready")
def ready():
    """Kubernetes/compose readiness probe: database reachable, warehouse built, models present, data fresh."""
    r = monitoring.readiness()
    return JSONResponse(r, status_code=200 if r["ready"] else 503)


@app.get("/metrics")
def metrics(authorization: str | None = Header(default=None)):
    token = os.environ.get("KD_METRICS_TOKEN")
    if token and authorization != f"Bearer {token}":
        raise HTTPException(401, "metrics token required")
    return Response(monitoring.metrics_text(), media_type="text/plain; version=0.0.4")


@app.get("/api/ops", dependencies=[Depends(require("system"))])
def operations():
    """Data & Models → Operations: readiness, live forecast accuracy, drift, jobs, backups, migrations."""
    return dict(**monitoring.snapshot(), backups=dict(**backup.status(), items=backup.listing()[:10], drills=backup.drills(5)),
                migrations=migrate.status())


@app.post("/api/ops/backup")
def ops_backup(body: dict | None = None, user=Depends(require("pipeline_run"))):
    return _enqueue("backup", user, dict(include_lake=bool((body or {}).get("include_lake"))))


@app.post("/api/ops/drill")
def ops_drill(body: dict | None = None, user=Depends(require("pipeline_run"))):
    return _enqueue("restore_drill", user, dict(name=(body or {}).get("name")))


@app.get("/health", include_in_schema=False)
@app.get("/api/health")
def health():
    out = dict(ok=True, as_of=str(AS_OF), ready=_ready())
    if out["ready"]:  # public headline numbers for the sign-in page (aggregates only, no operational detail)
        try:
            rc, sc = _card("reserve"), _card("shortfall")
            z = storage.read_table("reserve_zones")
            prov = storage.read_table("provenance")
            out["headline"] = dict(zones=int(len(z)), new_zones=int((z.status == "new").sum()),
                                   reserve_model=rc.get("champion"), gen_score=next((b["gen_score"] for b in rc.get("leaderboard", []) if b.get("champion")), None),
                                   occ_auc=rc.get("occurrence_check", {}).get("auc"), mape=sc["backtest"]["model_mape_30d"],
                                   real_feeds=int(prov.real.astype(bool).sum()), feeds=int(len(prov)))
        except Exception:  # noqa: BLE001 — health must never fail on a missing card
            pass
    return out


@app.get("/api/overview")
def overview(user=Depends(require("overview"))):
    fcs = {m["id"]: shortfall.forecast(m["id"]) for m in MINES}
    fy = shortfall.fiscal_summary()
    zones = storage.read_table("reserve_zones")
    al = _visible_alerts(user)
    month = {k: v["month"] for k, v in fcs.items()}
    tot_target = sum(v["target_t"] for v in month.values())
    tot_proj = sum(v["projected_t"] for v in month.values())
    new = zones[zones.status == "new"]
    return dict(
        as_of=str(AS_OF), month=str(AS_OF)[:7], fy=fy,
        kpis=dict(
            month_target_t=tot_target, month_projected_t=tot_proj, month_mtd_t=sum(v["mtd_actual_t"] for v in month.values()),
            month_gap_t=tot_target - tot_proj,
            mines_high_risk=sum(v["prob_miss"] >= 0.8 for v in month.values()),
            mines_medium_risk=sum(0.4 <= v["prob_miss"] < 0.8 for v in month.values()), mines=len(MINES),
            fy_ytd_actual_t=sum(r["ytd_actual_t"] for r in fy["mines"]), fy_ytd_target_t=sum(r["ytd_target_t"] for r in fy["mines"]),
            fy_projected_t=sum(r["fy_projected_t"] for r in fy["mines"]), fy_target_t=sum(r["fy_target_t"] for r in fy["mines"]),
            new_zones=int(len(new)), new_zones_high=int((new.priority == "High").sum()),
            new_zone_tonnes_mt=float(new.est_tonnes_mt.sum()), new_zone_area_km2=float(new.area_km2.sum()),
            open_alerts=int((al.status == "open").sum()), high_alerts=int(((al.status == "open") & (al.severity == "high")).sum()),
        ),
        mines=[dict(**{k: m[k] for k in ("id", "name", "district", "state", "method", "capacity_tpd", "lat", "lon")},
                    **{k: month[m["id"]][k] for k in ("target_t", "projected_t", "mtd_actual_t", "prob_miss", "expected_shortfall_t", "p10_t", "p90_t")})
               for m in MINES],
    )


# ---- GIS -------------------------------------------------------------------------------------
@app.get("/api/grid/meta", dependencies=[Depends(require("overview"))])
def grid_meta():
    df, cat = _load_grid()
    rows, cols = geo.grid()[0].shape
    layers = []
    for r in cat.to_dict("records"):
        v = df[r["name"]].values
        r = {k: (None if isinstance(x, float) and np.isnan(x) else x) for k, x in r.items()}
        if r.get("real") is not None:
            r["real"] = bool(r["real"])
        layers.append(dict(**r, min=float(np.nanpercentile(v, 1)), max=float(np.nanpercentile(v, 99))))
    legend = {int(r["code"]): r["name"] for r in storage.read_table("geology_legend").to_dict("records")}
    return dict(bounds=BOUNDS, res=RES, rows=rows, cols=cols, layers=layers, geology_legend=legend)


OVERVIEW_LAYERS = {"prospectivity"}  # what the Command Center map may show without 'reserves'
OVERVIEW_VECTORS = {"mines", "reserve_zones"}


@app.get("/api/grid/layer/{name}")
def grid_layer(name: str, user=Depends(require("overview"))):
    if name not in OVERVIEW_LAYERS and not auth.can(user, "reserves"):
        raise HTTPException(403, "Your role can't access detailed GIS layers.")
    df, cat = _load_grid()
    if name not in set(cat.name):
        raise HTTPException(404, "unknown layer")
    return dict(name=name, values=[None if np.isnan(x) else round(float(x), 4) for x in df[name].values])


@app.get("/api/grid/cell", dependencies=[Depends(require("reserves"))])
def grid_cell(lat: float, lon: float):
    rc = geo.cell_of(lat, lon)
    if not rc:
        raise HTTPException(404, "outside area of interest")
    df, cat = _load_grid()
    cid = rc[0] * geo.grid()[0].shape[1] + rc[1]
    row = df.iloc[cid]
    values = {n: (None if pd.isna(row[n]) else float(row[n])) for n in cat.name}
    holes = storage.read_table("boreholes")
    holes["d_km"] = np.hypot((holes.lat - row.lat) * 110.57, (holes.lon - row.lon) * 103.4)
    zones = storage.read_table("reserve_zones")
    zones["d_km"] = np.hypot((zones.centroid_lat - row.lat) * 110.57, (zones.centroid_lon - row.lon) * 103.4)
    return dict(cell_id=int(cid), lat=float(row.lat), lon=float(row.lon), values=values,
                explanation=reserve.explain_cell(int(cid)),
                nearest_boreholes=holes.nsmallest(3, "d_km").round(3).replace({np.nan: None}).to_dict("records"),
                nearest_zone=zones.nsmallest(1, "d_km").round(3).to_dict("records")[0] if len(zones) else None)


@app.get("/api/vector/{name}")
def vector(name: str, user=Depends(require("overview"))):
    if name not in {"reserve_zones", "mines", "boreholes", "known_deposits", "lineaments"}:
        raise HTTPException(404)
    if name not in OVERVIEW_VECTORS and not auth.can(user, "reserves"):
        raise HTTPException(403, "Your role can't access detailed GIS layers.")
    return storage.read_vector(name)


@app.get("/api/reserves", dependencies=[Depends(require("reserves"))])
def reserves():
    return dict(zones=storage.read_table("reserve_zones").to_dict("records"), model=_card("reserve"))


# ---- production ------------------------------------------------------------------------------
@app.get("/api/mines", dependencies=[Depends(require("overview"))])
def mines():
    return MINES


@app.get("/api/production/{mine_id}", dependencies=[Depends(require("production"))])
def production(mine_id: str, days: int = 120):
    _check(mine_id)
    return dict(mine=MINE_BY_ID[mine_id], history=shortfall.history(mine_id, days),
                forecast=shortfall.forecast(mine_id), drivers=shortfall.drivers(mine_id))


@app.get("/api/forecast", dependencies=[Depends(require("production"))])
def forecast_all():
    return dict(as_of=str(AS_OF), horizon_days=HORIZON_DAYS, model=_card("shortfall"),
                mines={m["id"]: shortfall.forecast(m["id"]) for m in MINES})


class WhatIf(BaseModel):
    mine_id: str
    excavator_avail: float | None = None
    equip_from_day: int | None = None
    fleet_ratio: float | None = None
    manpower_ratio: float | None = None
    blasts: float | None = None
    rain_mult: float | None = None
    extra_eff: float | None = None


@app.post("/api/whatif", dependencies=[Depends(require("optimize"))])
def whatif(body: WhatIf):
    _check(body.mine_id)
    o = {k: v for k, v in body.model_dump().items() if k != "mine_id" and v is not None}
    return optimize.whatif(body.mine_id, o)


class Optimise(BaseModel):
    budget_lakh: float = 40
    mines: list[str] | None = None


@app.post("/api/optimize", dependencies=[Depends(require("optimize_run"))])
def optimise(body: Optimise):
    return optimize.recommend(body.budget_lakh, body.mines)


@app.get("/api/actions", dependencies=[Depends(require("optimize"))])
def actions():
    return [{k: v for k, v in a.items() if k != "applies"} for a in optimize.ACTIONS]


# ---- alerts, reports, system -----------------------------------------------------------------
@app.get("/api/alerts")
def get_alerts(user=Depends(require("alerts"))):
    al = _visible_alerts(user)
    ids = set(al.alert_id)
    notes = [m for m in notify_outbox(user)["messages"] if m["kind"] != "alert" or m["ref"] in ids]
    return dict(alerts=al.to_dict("records"), notifications=notes[:100])


@app.post("/api/alerts/{alert_id}/ack")
def ack(alert_id: str, user=Depends(require("alerts_ack"))):
    if alert_id not in set(_visible_alerts(user).alert_id):
        raise HTTPException(404, "unknown alert")
    storage.execute("UPDATE alerts SET status='acknowledged' WHERE alert_id=?", (alert_id,))
    return dict(ok=True)


@app.post("/api/alerts/refresh", dependencies=[Depends(require("pipeline_run"))])
def refresh_alerts():
    return dict(alerts=alerts.generate())


def _csv(df, name):
    buf = io.StringIO(); df.to_csv(buf, index=False)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}_{AS_OF}.csv"'})


REPORT_PERMS = dict(forecast="production", production="production", reserve_zones="reserves",
                    boreholes="reserves", alerts="alerts")


@app.get("/api/reports/{name}.csv")
def report(name: str, user=Depends(require("reports"))):
    if name not in REPORT_PERMS:
        raise HTTPException(404)
    if not auth.can(user, REPORT_PERMS[name]):
        raise HTTPException(403, "Your role can't export this report.")
    if name == "forecast":
        rows = []
        for m in MINES:
            for d in shortfall.forecast(m["id"])["daily"]:
                rows.append(dict(mine_id=m["id"], mine=m["name"], **d))
        return _csv(pd.DataFrame(rows), name)
    if name == "production":
        h = storage.read_table("production_daily")
        return _csv(h[h.date >= str(pd.Timestamp(AS_OF) - pd.Timedelta(days=90))[:10]], name)
    if name == "reserve_zones":
        return _csv(storage.read_table("reserve_zones"), name)
    if name == "alerts":
        return _csv(_visible_alerts(user), name)
    if name == "boreholes":
        return _csv(storage.read_table("boreholes"), name)
    raise HTTPException(404)


@app.get("/api/system", dependencies=[Depends(require("system"))])
def system():
    runs = storage.read_table("pipeline_runs")
    runs["info"] = runs["info"].map(json.loads)
    return dict(as_of=str(AS_OF), inventory=storage.inventory(), runs=runs.to_dict("records"),
                ingestion=storage.read_table("ingestion_log").to_dict("records"),
                provenance=storage.read_table("provenance").replace({np.nan: None}).to_dict("records"),
                models=dict(reserve=_card("reserve"), shortfall=_card("shortfall")),
                backtest=storage.read_table("forecast_backtest").round(4).to_dict("records"), job=_job_view())


# ---- 3D terrain (diagram: "Interactive GIS Viewer · 3D / 2D") ------------------------------------
@app.get("/api/terrain/meta", dependencies=[Depends(require("reserves"))])
def terrain_meta(step: int = 2):
    if not terrain.available():
        raise HTTPException(404, "terrain stack not fetched yet (run the pipeline)")
    return terrain.meta(max(1, min(step, 5)))


@app.get("/api/terrain/elevation.bin", dependencies=[Depends(require("reserves"))])
def terrain_elevation(step: int = 2):
    return Response(terrain.elevation_bytes(max(1, min(step, 5))), media_type="application/octet-stream",
                    headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/terrain/texture/{kind}.png", dependencies=[Depends(require("reserves"))])
def terrain_texture(kind: str):
    try:
        return Response(terrain.texture_png(kind), media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})
    except KeyError:
        raise HTTPException(404)


# ---- PDF / Excel reports, archive, schedules (diagram: "Reports & Alerts", "PDF / Excel") ---------
def _report_allowed(user, name):
    return name in reports.REPORTS and all(auth.can(user, p) for p in reports.REPORTS[name]["perms"])


@app.get("/api/reports/catalog")
def report_catalog(user=Depends(require("reports"))):
    allowed = [k for k in reports.REPORTS if _report_allowed(user, k)]
    return dict(reports=[dict(name=k, **{x: v for x, v in reports.REPORTS[k].items() if x != "perms"}) for k in allowed],
                archive=[e for e in reports.archive() if e["report"] in allowed])


def _file(path, filename=None):
    media = "application/pdf" if str(path).endswith(".pdf") else \
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return FileResponse(path, media_type=media, filename=filename or os.path.basename(path))


@app.get("/api/reports/{name}.{fmt}")
def report_file(name: str, fmt: str, user=Depends(require("reports"))):
    if fmt == "csv":
        return report(name, user)
    if fmt not in ("pdf", "xlsx") or name not in reports.REPORTS:
        raise HTTPException(404)
    if not _report_allowed(user, name):
        raise HTTPException(403, "Your role can't generate this report.")
    entries, _ = reports.generate(name, (fmt,), by=user["username"])
    return _file(entries[0]["path"])


@app.get("/api/reports/archive/{name}/{filename}")
def report_archived(name: str, filename: str, user=Depends(require("reports"))):
    if not _report_allowed(user, name):
        raise HTTPException(403, "Your role can't open this report.")
    try:
        return _file(reports.archive_path(f"{name}/{filename}"))
    except FileNotFoundError:
        raise HTTPException(404)


class Schedule(BaseModel):
    name: str
    kind: str = "report"
    report: str | None = None
    formats: str = "pdf"
    every: str = "daily"
    weekday: int | None = None
    monthday: int | None = None
    at: str = "07:00"
    audiences: str = ""
    active: bool = True


@app.get("/api/schedules", dependencies=[Depends(require("reports"))])
def schedules_list():
    return scheduler.listing()


@app.post("/api/schedules")
def schedule_add(body: Schedule, user=Depends(require("notify_admin"))):
    try:
        sid = scheduler.add(body.model_dump())
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    auth.audit(user["username"], "schedule_add", body.name)
    return dict(ok=True, id=sid)


@app.patch("/api/schedules/{sid}")
def schedule_update(sid: int, body: dict, user=Depends(require("notify_admin"))):
    try:
        scheduler.update(sid, body)
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(400, str(e))
    auth.audit(user["username"], "schedule_update", f"{sid}: {', '.join(body)}")
    return dict(ok=True)


@app.delete("/api/schedules/{sid}")
def schedule_delete(sid: int, user=Depends(require("notify_admin"))):
    scheduler.delete(sid)
    auth.audit(user["username"], "schedule_delete", str(sid))
    return dict(ok=True)


@app.post("/api/schedules/{sid}/run")
def schedule_run(sid: int, user=Depends(require("notify_admin"))):
    try:
        status, detail = scheduler.run_now(sid)
    except KeyError:
        raise HTTPException(404)
    auth.audit(user["username"], "schedule_run", str(sid))
    return dict(status=status, detail=detail)


# ---- Email / SMS delivery ----------------------------------------------------------------------
@app.get("/api/notify/settings", dependencies=[Depends(require("notify_admin"))])
def notify_settings():
    return dict(settings=notify.settings_public(), recipients=notify.recipients(), audiences=notify.AUDIENCES)


@app.put("/api/notify/settings")
def notify_settings_put(body: dict, user=Depends(require("notify_admin"))):
    out = notify.update_settings(body)
    auth.audit(user["username"], "notify_settings", ", ".join(k for k in body if k in notify.DEFAULTS or k in notify.SECRETS))
    return out


class Recipient(BaseModel):
    name: str
    channel: str
    address: str
    audiences: list[str]
    min_severity: str = "high"


@app.post("/api/notify/recipients")
def notify_recipient_add(body: Recipient, user=Depends(require("notify_admin"))):
    try:
        rid = notify.add_recipient(body.name, body.channel, body.address, body.audiences, body.min_severity)
    except ValueError as e:
        raise HTTPException(400, str(e))
    auth.audit(user["username"], "recipient_add", f"{body.name} ({body.channel})")
    return dict(ok=True, id=rid)


@app.post("/api/notify/recipients/{rid}/active")
def notify_recipient_active(rid: int, body: dict, user=Depends(require("notify_admin"))):
    notify.set_recipient_active(rid, body.get("active", True))
    return dict(ok=True)


@app.delete("/api/notify/recipients/{rid}")
def notify_recipient_delete(rid: int, user=Depends(require("notify_admin"))):
    notify.delete_recipient(rid)
    auth.audit(user["username"], "recipient_delete", str(rid))
    return dict(ok=True)


@app.post("/api/notify/test")
def notify_test(body: dict, user=Depends(require("notify_admin"))):
    ch, addr = body.get("channel"), body.get("address", "")
    if ch not in ("email", "sms") or not addr:
        raise HTTPException(400, "channel (email|sms) and address required")
    notify.enqueue_test(ch, addr)
    stats = notify.flush()
    last = notify.outbox(1)["messages"][0]
    return dict(stats=stats, message=last)


@app.post("/api/notify/flush", dependencies=[Depends(require("notify_admin"))])
def notify_flush():
    return notify.flush()


@app.get("/api/notify/outbox")
def notify_outbox(user=Depends(require("alerts"))):
    out = notify.outbox(200)
    if not auth.can(user, "notify_admin"):  # other roles see delivery status, not people's addresses
        for m in out["messages"]:
            a = m["address"] or ""
            m["address"] = (a[:2] + "…" + a[a.find("@"):]) if "@" in a else (a[:3] + "…" + a[-2:])
            m["body"] = None
    return out


if os.environ.get("KD_DEV_SINKS") == "1":
    @app.post("/dev/sms/2010-04-01/Accounts/{sid}/Messages.json")
    async def dev_sms_sink(sid: str, request: Request):
        """Twilio-compatible SMS sink for demos (KD_DEV_SINKS=1 only): records to data/sms_sink.jsonl."""
        form = await request.form()
        return notify.record_sms_sink(sid, dict(form))


# ---- operational ingestion (diagram: "APIs", "IoT / Equipment Data") -------------------------
class Telemetry(BaseModel):
    records: list[dict]


@app.post("/api/ingest/telemetry")
def telemetry(body: Telemetry, x_device_key: str | None = Header(None)):
    """Device endpoint. Authenticated by X-Device-Key (not a user session); a device may only
    write records for the mine it is registered to. Invalid records are dead-lettered, not fatal."""
    dev = ingest.device_for_key(x_device_key)
    if not dev:
        raise HTTPException(401, "unknown or revoked device key")
    if len(body.records) > 5000:
        raise HTTPException(413, "max 5000 records per batch")
    return ingest.accept(body.records, channel="device", source=dev["device_id"], device_id=dev["device_id"],
                         allowed_mine=dev["mine_id"])


@app.get("/api/ingest/status", dependencies=[Depends(require("system"))])
def ingest_status():
    return dict(**ingest.status(), calibration=dict(
        periods=moil_public.as_table(), share_of_company=moil_public.SHARE_OF_COMPANY,
        mine_split=moil_public.MINE_SPLIT, plan_growth=moil_public.PLAN_GROWTH,
        note="Company totals are MOIL's published figures. The six mines' share and the per-mine split are assumptions."))


class NewDevice(BaseModel):
    device_id: str
    mine_id: str
    label: str = ""


@app.post("/api/ingest/devices", dependencies=[Depends(require("users"))])
def new_device(body: NewDevice):
    try:
        return dict(device_id=body.device_id, mine_id=body.mine_id,
                    key=ingest.create_device(body.device_id, body.mine_id, body.label),
                    note="Store this key now: it is shown once and only its hash is kept.")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/ingest/devices/{device_id}/revoke", dependencies=[Depends(require("users"))])
def revoke_device(device_id: str):
    ingest.revoke_device(device_id)
    return dict(ok=True)


@app.get("/api/equipment/live", dependencies=[Depends(require("production"))])
def equipment_live(minutes: int = 30):
    rows = ingest.live_equipment(minutes)
    by_mine = {}
    for r in rows:
        m = by_mine.setdefault(r["mine_id"], dict(mine_id=r["mine_id"], total=0, working=0, idle=0, breakdown=0, maintenance=0, last=r["ts"]))
        m["total"] += 1
        m[r["status"]] += 1
        m["last"] = max(m["last"], r["ts"])
    return dict(window_minutes=minutes, mines=list(by_mine.values()), machines=rows)


@app.get("/api/equipment/status", dependencies=[Depends(require("production"))])
def equipment_status(mine_id: str):
    _check(mine_id)
    return dict(**ingest.equipment_status(mine_id), feed=_feed_state())


# Virtual-mine live feed as a background service: it posts through the authenticated ingestion API
# exactly like an external device would (per-mine device keys, validation, dead-letter queue).
_feed = dict(thread=None, stop=None, started=None, log=[])


def _feed_state():
    t = _feed["thread"]
    return dict(running=bool(t and t.is_alive()), started=_feed["started"], log=_feed["log"][-5:])


def _feed_start():
    import threading as _th
    from . import virtual_mine
    if _feed["thread"] and _feed["thread"].is_alive():
        return
    stop = _th.Event()
    url = f"http://127.0.0.1:{os.environ.get('KD_PORT', '8000')}"

    def log(msg):
        _feed["log"] = (_feed["log"] + [f"{datetime.now():%H:%M:%S} {msg}"])[-50:]

    def run():
        try:
            virtual_mine.live(url, every=int(os.environ.get("KD_LIVE_EVERY", "20")), stop=stop, log=log)
        except Exception as e:  # surfaced in the panel
            log(f"stopped: {type(e).__name__}: {e}")

    _feed.update(stop=stop, started=datetime.now().isoformat(timespec="seconds"),
                 thread=_th.Thread(target=run, daemon=True, name="kd-virtual-mine-live"))
    _feed["thread"].start()


@app.post("/api/equipment/feed")
def equipment_feed(body: dict, user=Depends(require("pipeline_run"))):
    if body.get("action") == "start":
        _feed_start()
    elif body.get("action") == "stop" and _feed["stop"]:
        _feed["stop"].set()
    else:
        raise HTTPException(400, "action must be start or stop")
    auth.audit(user["username"], f"live_feed_{body['action']}")
    return _feed_state()


# ---- manual uploads (diagram: "Manual Uploads (Drilling, Maps)") -----------------------------
MAX_UPLOAD = 60 * 1024 * 1024


@app.get("/api/uploads", dependencies=[Depends(require("data_upload"))])
def list_uploads():
    return dict(kinds=uploads.KINDS, history=uploads.history())


@app.post("/api/uploads", dependencies=[Depends(require("data_upload"))])
async def upload(request: Request, kind: str = Form(...), file: UploadFile = File(...), user=Depends(current_user)):
    raw = await file.read()
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "file larger than 60 MB")
    try:
        return uploads.ingest_upload(kind, file.filename or "upload", raw, user["username"])
    except uploads.UploadError as e:
        raise HTTPException(400, str(e))


@app.get("/api/uploads/template/{kind}.csv", dependencies=[Depends(require("data_upload"))])
def upload_template(kind: str):
    if kind not in ("boreholes", "geochem", "operations"):
        raise HTTPException(404)
    return Response(uploads.template(kind), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{kind}_template.csv"'})


@app.post("/api/uploads/{upload_id}/deactivate", dependencies=[Depends(require("data_upload"))])
def deactivate_upload(upload_id: str):
    uploads.deactivate(upload_id)
    return dict(ok=True)


def _enqueue(kind, user, params=None):
    jid, new = jobs.enqueue(kind, params, by=user["username"] if user else "system")
    if new and user:
        auth.audit(user["username"], f"job_{kind}", str(jid))
    return _job_view(jobs.get(jid))


@app.post("/api/pipeline/run")
def run_pipeline(user=Depends(require("pipeline_run"))):
    return _enqueue("pipeline", user)


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require("system"))])
def job_status(job_id: int):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404)
    return _job_view(j)


# ---- model registry (diagram: Layer 4 model management) --------------------------------------
@app.get("/api/models", dependencies=[Depends(require("system"))])
def models_list():
    return dict(registry=registry.listing(), leaderboards={t: _card(t).get("leaderboard", []) for t in ("reserve", "shortfall")},
                serving=dict(shortfall=shortfall.serving_family(), reserve=_card("reserve").get("champion")), job=_job_view())


def _after_champion_change(task, user, action, detail):
    auth.audit(user["username"], f"model_{action}", f"{task}: {detail}")
    if task == "shortfall":
        shortfall.sync_card(); shortfall.reload()
        alerts.generate()  # shortfall alerts depend on the serving forecaster
        return dict(ok=True, champion=registry.champion(task)[0], job=None)
    # reserve zones are derived from the champion's probability map: re-map in the background
    # (U-Net grids are re-used, so this takes well under a minute)
    job = _enqueue("reserve_remap", user)
    return dict(ok=True, champion=registry.champion(task)[0], job=job)


def _task(task):
    if task not in registry.PRIMARY:
        raise HTTPException(404, "unknown task")


@app.post("/api/models/{task}/{version}/promote")
def model_promote(task: str, version: str, user=Depends(require("models"))):
    _task(task)
    if _job_view()["running"]:
        raise HTTPException(409, "a job is running; try again when it finishes")
    try:
        registry.promote(task, version, manual=True)
    except KeyError:
        raise HTTPException(404, "unknown version")
    return _after_champion_change(task, user, "promote", version)


@app.post("/api/models/{task}/rollback")
def model_rollback(task: str, user=Depends(require("models"))):
    _task(task)
    if _job_view()["running"]:
        raise HTTPException(409, "a job is running; try again when it finishes")
    try:
        v = registry.rollback(task)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    return _after_champion_change(task, user, "rollback", v)


@app.post("/api/models/{task}/unpin")
def model_unpin(task: str, user=Depends(require("models"))):
    _task(task)
    registry.unpin(task)
    auth.audit(user["username"], "model_unpin", task)
    return dict(ok=True, note="the next training run will promote the leaderboard winner")


def _check(mine_id):
    if mine_id not in MINE_BY_ID:
        raise HTTPException(404, "unknown mine")


# Serve the built frontend if present (single-process deploy).
DIST = ROOT.parent / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        f = DIST / path
        return FileResponse(f if path and f.is_file() else DIST / "index.html")
