"""Layer 4 · Production Shortfall Prediction.

A driver-based gradient-boosting model learns daily output (as a fraction of capacity) from
the things that actually move it: equipment availability, fleet, manpower, blasting, rain and
ground wetness, festivals, season and trend. Three models give P10 / P50 / P90.

Forecasting = run the model on a *scenario* of future drivers:
  weather     IMD extended-range forecast (30 days)
  equipment   last week's level, mean-reverting to the 90-day norm (half-life 7 days)
  manpower    recent attendance, with known festival days
  blasting    the plan, respecting monsoon limits and heavy-rain stand-downs
The same scenario machinery powers what-if analysis and the optimiser.

Backtest: rolling origins, 30-day horizon, drivers frozen at the origin (rain from
climatology). That's the honest test, because it's what the system knows on the day.
A seasonal-naive baseline (last year x recent trend, the Prophet-style seasonal prior)
is scored alongside so the gain is measurable.
"""
import json
from datetime import date, timedelta

import joblib
import numpy as np
import pandas as pd
from scipy.stats import norm
from . import forecasters, storage
from .config import AS_OF, HORIZON_DAYS, MINE_BY_ID, MINES, MODELS, SEED
from .processing import PRODUCTION_FEATURES, production_features

RHO = 0.3  # assumed correlation of daily forecast errors when summing to a month
NOMINAL = dict(excavator_avail=0.91, fleet_ratio=0.95, manpower_ratio=0.93)


def _fit(df):
    return forecasters.gbm_fit(df)


def _predict(models, rows):
    return forecasters.predict("gbm", models, rows, None)


# ---- scenario construction -------------------------------------------------------------------
def build_scenario(hist, mine_id, start, days, rain=None, overrides=None):
    """Future driver rows for one mine. hist = integrated daily table up to `start`."""
    o = overrides or {}
    m = MINE_BY_ID[mine_id]
    h = hist[(hist.mine_id == mine_id) & (hist.date < str(start))]
    dates = [start + timedelta(days=k) for k in range(days)]
    oc = m["method"] == "opencast"

    decay = 0.5 ** (np.arange(days) / 7)

    def revert(col):
        now, norm_ = float(h[col].tail(7).mean()), float(h[col].tail(90).mean())
        return norm_ + (now - norm_) * decay

    equip = revert("excavator_avail")
    ramp_day = int(o.get("equip_from_day", 0))
    if "excavator_avail" in o:
        equip[ramp_day:] = o["excavator_avail"]
    fleet = np.minimum(1.0, revert("fleet_ratio") if "fleet_ratio" not in o else np.full(days, o["fleet_ratio"]))
    nonfest = h[h.festival == 0].tail(30)
    manpower = np.full(days, o.get("manpower_ratio", float(nonfest.manpower_ratio.mean())))

    rain = np.zeros(days) if rain is None else np.asarray(rain, dtype=float)[:days]
    rain = rain * o.get("rain_mult", 1.0)
    from .config import FESTIVALS
    festival = np.array([str(d) in FESTIVALS for d in dates], dtype=int)
    in_monsoon = np.array([(6, 15) <= (d.month, d.day) <= (9, 15) for d in dates])
    planned = o.get("blasts", 2 if oc else 3)
    blasts = np.full(days, float(planned))
    if oc:
        blasts[in_monsoon] = np.minimum(blasts[in_monsoon], 1)
        blasts[rain > 30] = 0

    api0 = float(h.api.iloc[-1]) if len(h) else 0.0
    prev0 = float(h.rain_mm.iloc[-1]) if len(h) else 0.0
    api, rp = np.zeros(days), np.zeros(days)
    for k in range(days):
        rp[k] = prev0 if k == 0 else rain[k - 1]
        api[k] = (api0 if k == 0 else api[k - 1]) * 0.85 + rain[k]

    plan = _table("monthly_plan")
    plan = plan[plan.mine_id == mine_id].set_index("month").target_t
    target = np.array([plan.get(str(d)[:7], np.nan) / pd.Period(str(d)[:7]).days_in_month for d in dates])
    return pd.DataFrame(dict(date=[str(d) for d in dates], mine_id=mine_id, method=m["method"],
                             capacity_tpd=m["capacity_tpd"], rain_mm=rain, rain_prev=rp, api=api,
                             excavator_avail=equip, fleet_ratio=fleet, manpower_ratio=manpower,
                             blasts=blasts, festival=festival, target_t=target))


def _climatology_rain(hist, mine_id, start, days):
    h = hist[(hist.mine_id == mine_id) & (hist.date < str(start))].copy()
    h["doy"] = pd.to_datetime(h.date).dt.dayofyear
    clim = h.groupby("doy").rain_mm.mean().reindex(range(1, 367)).interpolate().bfill().ffill()
    clim = clim.rolling(15, center=True, min_periods=1).mean()
    return np.array([clim[(start + timedelta(days=k)).timetuple().tm_yday] for k in range(days)])


def _forecast_rain(mine_id, start, days):
    mines = _table("mines").set_index("id")
    fc = _table("weather_forecast")
    st = mines.loc[mine_id, "station"]
    f = fc[fc.station == st].set_index("date").rain_mm
    return np.array([f.get(str(start + timedelta(days=k)), 0.0) for k in range(days)])


_tables = {}


def _table(name):
    """Small reference tables, cached: forecasts are called hundreds of times by the optimiser."""
    if name not in _tables:
        _tables[name] = storage.read_table(name)
    return _tables[name]


# ---- training + backtest ---------------------------------------------------------------------
def _seasonal_naive(hist, mine_id, start, days):
    h = hist[hist.mine_id == mine_id].set_index("date").actual_t
    ly = [str(start - timedelta(days=365) + timedelta(days=k)) for k in range(days)]
    recent = h[(h.index < str(start)) & (h.index >= str(start - timedelta(days=90)))].sum()
    recent_ly = h[(h.index < str(start - timedelta(days=365))) & (h.index >= str(start - timedelta(days=455)))].sum()
    return h.reindex(ly).values * (recent / recent_ly if recent_ly else 1.0)


def _origins(n=6):
    """The last n month starts whose 30-day window is fully observed."""
    first = date(AS_OF.year, AS_OF.month, 1)
    out = []
    for k in range(1, 14):
        o = (pd.Period(first, "M") - k).to_timestamp().date()
        if o + timedelta(days=HORIZON_DAYS) <= AS_OF:
            out.append(o)
        if len(out) == n:
            break
    return sorted(out)


def _data_version(hist):
    import hashlib
    sig = f"{len(hist)}|{hist.date.max()}|{hist.actual_t.sum():.0f}"
    return f"{hist.date.max()}-{hashlib.sha1(sig.encode()).hexdigest()[:8]}"


def _calibrate_intervals(family, hist, days=120):
    """Conformalized quantile regression (CQR) for the daily P10-P90 band. Fit on history before the
    last `days`, score how far actuals fall outside the band (in utilisation units), and widen by the
    80th percentile of that score. Coverage is reported on the second half, calibrated on the first."""
    last = pd.Timestamp(hist.date.max())
    cut, mid = str((last - pd.Timedelta(days=days)).date()), str((last - pd.Timedelta(days=days // 2)).date())
    m = forecasters.fit(family, hist[hist.date < cut])
    test = hist[hist.date >= cut].reset_index(drop=True)
    p10, _, p90 = forecasters.predict(family, m, test, hist)
    cap = test.capacity_tpd.values
    u = test.actual_t.values / cap
    e = np.maximum(p10 / cap - u, u - p90 / cap)
    first = (test.date < mid).values

    def q(x):
        return float(np.quantile(x, min(1.0, 0.8 * (len(x) + 1) / len(x))))

    return dict(interval_widen_util=round(q(e), 4), coverage_raw=round(float((e[~first] <= 0).mean()), 3),
                coverage_calibrated=round(float((e[~first] <= q(e[first])).mean()), 3))


def train(log=print):
    from . import registry
    hist = storage.read_table("production_daily")
    origins = _origins()
    fams = forecasters.available()
    bt_rows = []
    for o in origins:
        train_df = hist[hist.date < str(o)]
        models = {f: forecasters.fit(f, train_df) for f in fams}
        for m in MINES:
            rows = build_scenario(train_df, m["id"], o, HORIZON_DAYS, rain=_climatology_rain(train_df, m["id"], o, HORIZON_DAYS))
            actual = hist[(hist.mine_id == m["id"]) & (hist.date >= str(o))].head(HORIZON_DAYS).actual_t.values
            naive = _seasonal_naive(hist, m["id"], o, HORIZON_DAYS)
            r = dict(origin=str(o), mine_id=m["id"], actual_30d=actual.sum(), naive_30d=np.nansum(naive),
                     naive_daily_mae=np.nanmean(np.abs(naive - actual)))
            for f in fams:
                _, p50, _ = forecasters.predict(f, models[f], rows, train_df)
                r[f"{f}_30d"] = p50.sum()
                r[f"{f}_daily_mae"] = np.mean(np.abs(p50 - actual))
            bt_rows.append(r)
        cur = [x for x in bt_rows if x["origin"] == str(o)]
        log(f"      backtest origin {o}: " + "  ".join(
            f"{f} {np.mean([abs(x[f + '_30d'] - x['actual_30d']) / x['actual_30d'] for x in cur]) * 100:.1f}%" for f in fams))
    bt = pd.DataFrame(bt_rows)
    bt["naive_ape"] = (bt.naive_30d - bt.actual_30d).abs() / bt.actual_30d
    naive_mape = round(float(bt.naive_ape.mean() * 100), 2)
    board = {}
    for f in fams:
        bt[f"{f}_ape"] = (bt[f"{f}_30d"] - bt.actual_30d).abs() / bt.actual_30d
        board[f] = dict(mape_30d=round(float(bt[f"{f}_ape"].mean() * 100), 2),
                        rel_rmse_30d=round(float(np.sqrt((((bt[f"{f}_30d"] - bt.actual_30d) / bt.actual_30d) ** 2).mean())), 4),
                        daily_mae_t=round(float(bt[f"{f}_daily_mae"].mean()), 1),
                        beats_naive=bool(bt[f"{f}_ape"].mean() * 100 < naive_mape))

    # Explanatory fit with the true drivers on the last 120 days (how well drivers explain output).
    cut = str(AS_OF - timedelta(days=120))
    mdl = _fit(hist[hist.date < cut])
    test = hist[hist.date >= cut]
    lo, p50, hi = _predict(mdl, test)
    resid = test.actual_t.values - p50
    r2 = 1 - (resid ** 2).sum() / ((test.actual_t - test.groupby("mine_id").actual_t.transform("mean")) ** 2).sum()
    coverage = float(((test.actual_t.values >= lo) & (test.actual_t.values <= hi)).mean())

    # Final fits on all history, registered as candidates; the best backtest MAPE serves.
    dv = _data_version(hist)
    finals, versions = {}, {}
    for f in fams:
        finals[f] = forecasters.fit(f, hist, log=log if f == "lstm" else (lambda *_: None))
        cal = _calibrate_intervals(f, hist)
        log(f"      {f}: P10-P90 coverage {cal['coverage_raw']:.0%} raw -> {cal['coverage_calibrated']:.0%} after CQR (held-out 60 days)")
        board[f].update(coverage_calibrated=cal["coverage_calibrated"])
        mt = dict(board[f], naive_mape_30d=naive_mape, **cal)
        versions[f], _, _ = registry.register("shortfall", f, finals[f], dict(algorithm=forecasters.ALGO[f], metrics=mt,
                                              origins=[str(o) for o in origins]), mt, dv, auto_promote=False)
    champ_fam, why = registry.select_champion(
        "shortfall", {f: dict(version=versions[f], metrics=board[f]) for f in fams}, dv)
    champ, _ = registry.champion("shortfall")
    log(f"      shortfall champion: {champ_fam} | {why}")
    leaderboard = [dict(name=f, algorithm=forecasters.ALGO[f], version=versions[f], champion=f == champ_fam, **board[f])
                   for f in fams]
    leaderboard.append(dict(name="seasonal_naive", algorithm="Same window last year x 90-day trend (baseline)",
                            version=None, champion=False, mape_30d=naive_mape,
                            daily_mae_t=round(float(bt.naive_daily_mae.mean()), 1)))

    MODELS.mkdir(exist_ok=True)
    joblib.dump(finals["gbm"], MODELS / "shortfall.joblib")  # explanation model + fallback
    imp = _permutation_importance(finals["gbm"]["p50"], hist.tail(1500))
    cb = board[champ_fam]
    per_origin = bt.groupby("origin")[[f"{f}_ape" for f in fams] + ["naive_ape"]].mean().mul(100).round(2)
    per_origin["model_ape"] = per_origin[f"{champ_fam}_ape"]
    card = dict(
        name="Production shortfall forecaster",
        algorithm=forecasters.ALGO[champ_fam], champion=champ_fam, champion_version=champ["version"],
        leaderboard=leaderboard,
        baseline="Seasonal naive (same window last year × 90-day trend)",
        backtest=dict(origins=[str(o) for o in origins], horizon_days=HORIZON_DAYS,
                      model_mape_30d=cb["mape_30d"], naive_mape_30d=naive_mape, rel_rmse_30d=cb["rel_rmse_30d"],
                      model_daily_mae_t=cb["daily_mae_t"], naive_daily_mae_t=round(float(bt.naive_daily_mae.mean()), 1),
                      per_origin=per_origin.reset_index().to_dict("records")),
        driver_fit=dict(window="last 120 days, true drivers (GBM)", r2_within_mine=round(float(r2), 3),
                        p10_p90_coverage=round(coverage, 3)),
        intervals=dict(method="Conformalized quantile regression (CQR): daily band widened by the 80th percentile of "
                              "out-of-band errors on a held-out window; coverage measured on the following 60 days",
                       **{k: champ["metrics"].get(k) for k in ("interval_widen_util", "coverage_raw", "coverage_calibrated")}),
        explanation_model="gradient boosting (driver importance is computed on the GBM for every champion)",
        importance=imp, rows=len(hist), features=PRODUCTION_FEATURES,
    )
    (MODELS / "shortfall_card.json").write_text(json.dumps(card, indent=2))
    bt["model_30d"], bt["model_ape"] = bt[f"{champ_fam}_30d"], bt[f"{champ_fam}_ape"]
    storage.write_table("forecast_backtest", bt)
    storage.log_run(4, "shortfall models", champion=champ_fam, mape=cb["mape_30d"], naive_mape=naive_mape,
                    **{f"{f}_mape": board[f]["mape_30d"] for f in fams})
    return card


LABELS = dict(mine_code="Mine", is_opencast="Opencast", doy_sin="Season", doy_cos="Season", t_years="Trend",
              rain_mm="Rainfall", rain_prev="Rainfall (prev day)", api="Ground wetness", excavator_avail="Equipment availability",
              fleet_ratio="Dumper fleet", manpower_ratio="Manpower", blasts="Blasting", festival="Festival")


def _permutation_importance(model, df):
    rng = np.random.default_rng(SEED)
    X = production_features(df); y = (df.actual_t / df.capacity_tpd).values
    base = np.mean((model.predict(X) - y) ** 2)
    out = {}
    for c in X.columns:
        Xp = X.copy(); Xp[c] = rng.permutation(Xp[c].values)
        out[LABELS[c]] = out.get(LABELS[c], 0) + float(np.mean((model.predict(Xp) - y) ** 2) - base)
    tot = sum(max(v, 0) for v in out.values())
    return sorted([dict(feature=k, value=round(max(v, 0) / tot, 4)) for k, v in out.items()], key=lambda d: -d["value"])


def sync_card():
    """After an admin promote/rollback: point the model card at the registry's current champion."""
    from . import registry
    champ, _ = registry.champion("shortfall")
    p = MODELS / "shortfall_card.json"
    card = json.loads(p.read_text())
    card.update(champion=champ["algo"], champion_version=champ["version"],
                algorithm=forecasters.ALGO.get(champ["algo"], champ["algo"]))
    for b in card.get("leaderboard", []):
        b["champion"] = b.get("version") == champ["version"]
    m = champ["metrics"]
    card["backtest"].update(model_mape_30d=m.get("mape_30d"), rel_rmse_30d=m.get("rel_rmse_30d"),
                            model_daily_mae_t=m.get("daily_mae_t"))
    p.write_text(json.dumps(card, indent=2))


# ---- serving ---------------------------------------------------------------------------------
_state = {}


def _ctx():
    """Serving model = the registry champion (any family); falls back to the GBM file."""
    if "models" not in _state:
        from . import registry
        champ, _ = registry.champion("shortfall")
        try:
            _state["family"], _state["models"] = champ["algo"], registry.load_artifact("shortfall")
            _state["rel_rmse"] = champ["metrics"]["rel_rmse_30d"]
            _state["widen"] = champ["metrics"].get("interval_widen_util", 0.0)
        except Exception:
            _state["family"], _state["models"] = "gbm", joblib.load(MODELS / "shortfall.joblib")
        _state["hist"] = storage.read_table("production_daily")
    return _state["models"], _state["hist"]


def serving_family():
    _ctx()
    return _state["family"]


def reload():
    _state.clear()
    _tables.clear()


def _agg_sigma(p10, p50, p90):
    """Uncertainty of a multi-day total: the quantile spread (noise given the drivers) plus
    the backtested 30-day error, which carries the uncertainty in the drivers themselves."""
    s = (p90 - p10) / 2.563
    noise = RHO * s.sum() ** 2 + (1 - RHO) * (s ** 2).sum()
    if "rel_rmse" not in _state:
        _state["rel_rmse"] = json.loads((MODELS / "shortfall_card.json").read_text())["backtest"]["rel_rmse_30d"]
    driver = (_state["rel_rmse"] * p50.sum() * np.sqrt(len(p50) / HORIZON_DAYS)) ** 2
    return float(np.sqrt(noise + driver))


def _risk(mu, sigma, target):
    z = (target - mu) / max(sigma, 1e-6)
    return dict(prob_miss=float(norm.cdf(z)), expected_shortfall_t=float((target - mu) * norm.cdf(z) + sigma * norm.pdf(z)))


def forecast(mine_id, overrides=None, days=HORIZON_DAYS):
    models, hist = _ctx()
    start = date.fromisoformat(hist.date.max()) + timedelta(days=1)  # first day not yet fully observed
    rows = build_scenario(hist, mine_id, start, days, rain=_forecast_rain(mine_id, start, days), overrides=overrides)
    p10, p50, p90 = forecasters.predict(_state["family"], models, rows, hist)
    boost = 1 + (overrides or {}).get("extra_eff", 0.0)  # e.g. schedule change to a richer bench
    p10, p50, p90 = p10 * boost, p50 * boost, p90 * boost
    rows = rows.assign(p10=p10, p50=p50, p90=p90)

    # Current month: month-to-date actuals + forecast for the rest of the month.
    month = str(AS_OF)[:7]
    h = hist[hist.mine_id == mine_id]
    mtd = h[h.date.str.startswith(month)]
    rest = rows[rows.date.str.startswith(month)]
    plan = _table("monthly_plan")
    m_target = float(plan[(plan.mine_id == mine_id) & (plan.month == month)].target_t.iloc[0])
    m_mu = float(mtd.actual_t.sum() + rest.p50.sum())
    m_sigma = _agg_sigma(rest.p10.values, rest.p50.values, rest.p90.values)
    month_risk = _risk(m_mu, m_sigma, m_target)

    h_target = float(rows.target_t.sum())
    h_risk = _risk(float(p50.sum()), _agg_sigma(p10, p50, p90), h_target)
    # Displayed daily band: conformal widening (aggregates above use the raw heads + backtested driver error)
    w = _state.get("widen", 0.0) * rows.capacity_tpd.values
    rows = rows.assign(p10=np.clip(rows.p10 - w, 0, None), p90=rows.p90 + w)
    return dict(
        mine_id=mine_id, as_of=str(AS_OF), data_through=hist.date.max(),
        daily=rows[["date", "p10", "p50", "p90", "target_t", "rain_mm", "excavator_avail", "fleet_ratio",
                    "manpower_ratio", "blasts", "festival"]].round(3).to_dict("records"),
        month=dict(month=month, target_t=m_target, mtd_actual_t=float(mtd.actual_t.sum()), projected_t=m_mu,
                   p10_t=m_mu - 1.2816 * m_sigma, p90_t=m_mu + 1.2816 * m_sigma, **month_risk),
        horizon=dict(days=days, target_t=h_target, forecast_t=float(p50.sum()), **h_risk),
    )


def drivers(mine_id):
    """Where the 30-day shortfall comes from: gain from restoring each driver to nominal."""
    base = forecast(mine_id)["horizon"]["forecast_t"]
    m = MINE_BY_ID[mine_id]
    tests = {
        "Equipment availability": dict(excavator_avail=NOMINAL["excavator_avail"]),
        "Dumper fleet": dict(fleet_ratio=NOMINAL["fleet_ratio"]),
        "Manpower": dict(manpower_ratio=NOMINAL["manpower_ratio"]),
        "Weather (rain)": dict(rain_mult=0.0),
    }
    out = []
    for name, ov in tests.items():
        gain = forecast(mine_id, ov)["horizon"]["forecast_t"] - base
        out.append(dict(driver=name, impact_t=round(gain, 0)))
    out.sort(key=lambda d: -d["impact_t"])
    return dict(mine_id=mine_id, method=m["method"], baseline_t=base, drivers=out)


def history(mine_id, days=120):
    _, hist = _ctx()
    h = hist[hist.mine_id == mine_id].tail(days)
    return h[["date", "actual_t", "target_t", "rain_mm", "excavator_avail", "fleet_ratio", "manpower_ratio",
              "blasts", "downtime_hrs"]].round(3).to_dict("records")


def fiscal_summary():
    """FY (Apr-Mar) to date, per mine and total, with a simple full-year projection."""
    _, hist = _ctx()
    fy_start = date(AS_OF.year if AS_OF.month >= 4 else AS_OF.year - 1, 4, 1)
    plan = _table("monthly_plan")
    fy_months = [str(p) for p in pd.period_range(fy_start, periods=12, freq="M")]
    out = []
    for m in MINES:
        h = hist[(hist.mine_id == m["id"]) & (hist.date >= str(fy_start))]
        ytd_a, ytd_t = float(h.actual_t.sum()), float(h.target_t.sum())
        fy_t = float(plan[(plan.mine_id == m["id"]) & plan.month.isin(fy_months)].target_t.sum())
        fc = forecast(m["id"])
        ratio = ytd_a / ytd_t if ytd_t else 1
        remaining_t = fy_t - ytd_t - fc["horizon"]["target_t"]
        proj = ytd_a + fc["horizon"]["forecast_t"] + max(remaining_t, 0) * ratio
        out.append(dict(mine_id=m["id"], name=m["name"], ytd_actual_t=ytd_a, ytd_target_t=ytd_t,
                        fy_target_t=fy_t, fy_projected_t=proj, attainment_ytd=ratio))
    return dict(fy=f"FY {fy_start.year}-{str(fy_start.year + 1)[2:]}", mines=out)
