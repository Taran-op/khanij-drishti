"""Virtual mine: a stand-in for the field systems that real mines would connect.

It behaves like a fleet of devices: it holds per-mine API keys and pushes operational telemetry
into the ingestion API, exactly as a real fleet-management system or shift logger would.
Nothing it produces enters the platform except through that API's validation.

    python -m kd.virtual_mine provision                 # create one device key per mine (stored locally)
    python -m kd.virtual_mine backfill [--http URL]     # history since 2023 (direct, or over HTTP)
    python -m kd.virtual_mine live --http URL [--every 20]   # heartbeats + shift tonnes as the day runs

The operations are driven by REAL inputs where they exist: ERA5 rainfall at each mine (ground
wetness, opencast stoppages), the festival calendar, the monsoon blasting limit. Output is then
calibrated so the six mines' totals match MOIL's PUBLISHED company production for every period
it reports (kd/moil_public.py); the per-mine split is an assumption, labelled as such.
"""
import argparse
import json
import sys
import time
import zlib
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from . import ingest
from . import moil_public as mp
from .config import DATA, FESTIVALS, HISTORY_START, LAKE, MINES, SEED

KEYS = DATA / ".virtual_mine_keys.json"
SHIFTS = (("A", 6), ("B", 14), ("C", 22))  # shift, start hour
SHIFT_SPLIT = np.array([0.40, 0.35, 0.25])


def _log(msg):
    print(f"[virtual-mine] {msg}", flush=True)


# ---- the simulation -------------------------------------------------------------------------
def _rain(days):
    """Real ERA5 rain per mine from the lake; today (not yet in ERA5) from the forecast, else 0."""
    out = {}
    p = LAKE / "external" / "imd_station_daily_rain.csv"
    f = LAKE / "external" / "imd_extended_range_forecast.csv"
    hist = pd.read_csv(p) if p.exists() else pd.DataFrame(columns=["station", "date", "rain_mm"])
    fc = pd.read_csv(f) if f.exists() else pd.DataFrame(columns=["station", "date", "rain_mm"])
    both = pd.concat([hist[["station", "date", "rain_mm"]], fc[["station", "date", "rain_mm"]]]).drop_duplicates(["station", "date"])
    idx = [str(d) for d in days]
    for m in MINES:
        s = both[both.station == m["id"]].set_index("date").rain_mm
        out[m["id"]] = s.reindex(idx).fillna(0).values.astype(float)
    return out, bool(len(hist))


def simulate(end):
    """Deterministic daily operations for every mine, HISTORY_START..end inclusive."""
    rng = np.random.default_rng(SEED + 7)
    days = [HISTORY_START + timedelta(days=i) for i in range((end - HISTORY_START).days + 1)]
    n = len(days)
    years = np.array([(d - HISTORY_START).days / 365.25 for d in days])
    fest = np.array([str(d) in FESTIVALS for d in days])
    monsoon = np.array([(6, 15) <= (d.month, d.day) <= (9, 15) for d in days])
    rains, real_rain = _rain(days)
    out = {}
    for m in MINES:
        oc = m["method"] == "opencast"
        cap, nd = m["capacity_tpd"], m["dumpers"]
        rain = rains[m["id"]]
        api = np.zeros(n)
        for i in range(n):
            api[i] = (api[i - 1] * 0.85 if i else 0) + rain[i]
        base = np.full(n, 0.91)
        if m["id"] == "TRD":  # ageing excavator fleet from mid-2025
            s = (date(2025, 6, 1) - HISTORY_START).days
            if s < n:
                base[s:] = np.linspace(0.89, 0.78, n - s)
        events = np.zeros(n)
        i = 0
        while i < n:
            if rng.random() < 0.025:
                L = int(rng.geometric(1 / 5)); events[i:i + L] += rng.uniform(0.12, 0.35); i += L
            i += 1
        if m["id"] == "DGB":  # a major breakdown running into the present
            events[max(0, n - 10):] += 0.30
        avail = np.clip(base - events + rng.normal(0, 0.02, n), 0.3, 0.98)
        fleet = np.clip(np.round(nd * (avail * 1.02 + rng.normal(0, 0.04, n))), 0, nd)
        blasts = np.full(n, 2.0 if oc else 3.0)
        if oc:
            blasts[monsoon] = 1          # monsoon blasting limit (regulatory)
            blasts[rain > 30] = 0        # heavy-rain stand-down
        skip = rng.random(n) < 0.08
        blasts[skip] = np.maximum(0, blasts[skip] - 1)
        rostered = int(cap / 4.5)
        present = np.clip(0.93 + rng.normal(0, 0.025, n) - 0.25 * fest - 0.03 * ((rain > 40) & oc), 0.4, 1.0)
        rain_eff = np.exp(-0.008 * rain - 0.006 * np.maximum(api - 30, 0)) if oc else \
            np.exp(-0.0015 * rain - 0.001 * np.maximum(api - 60, 0))
        blast_eff = 0.70 + 0.15 * np.minimum(blasts, 2) if oc else 0.80 + 0.20 * np.minimum(blasts / 3, 1)
        eff = (rain_eff * avail ** 1.1 * np.minimum(1, fleet / (0.85 * nd)) ** 0.6 * present ** 0.8 * blast_eff
               * 1.035 ** years * rng.lognormal(0, 0.05, n))
        out[m["id"]] = pd.DataFrame(dict(
            date=[str(d) for d in days], raw=cap * eff, avail=avail, fleet=fleet.astype(int), dumpers=nd,
            blasts=blasts.astype(int), explosive=np.round(blasts * rng.uniform(900, 1300, n)), rostered=rostered,
            present=np.round(rostered * present).astype(int), shares=list(rng.dirichlet(SHIFT_SPLIT * 150, n))))
    _calibrate(out, days)
    _plans(out, end)
    for mid in out:  # O(1) day lookups for record building
        plan = out[mid].attrs["plan"]
        out[mid] = out[mid].set_index("date", drop=False)
        out[mid].attrs["plan"] = plan
    return out, real_rain


def _calibrate(out, days):
    """Scale each mine so that, period by period, it delivers its assumed share of MOIL's published
    company output. Days after the latest published month keep the last period's factors, so
    present-day disruptions (e.g. a breakdown) still show up instead of being calibrated away."""
    dts = [date.fromisoformat(d) for d in out[MINES[0]["id"]].date]
    keys = [mp.period_of(d) for d in dts]
    for mid, df in out.items():
        target = np.array([mp.daily_company_tonnes(d) * mp.SHARE_OF_COMPANY * mp.MINE_SPLIT[mid] for d in dts])
        tonnes = np.zeros(len(df))
        factor = {}
        groups = pd.Series(range(len(df))).groupby([k[0] for k in keys])
        for start, idx in groups.groups.items():
            if start <= mp.LATEST_PUBLISHED:
                idx = np.array(list(idx))
                factor[start] = target[idx].sum() / max(df.raw.values[idx].sum(), 1e-9)
                tonnes[idx] = df.raw.values[idx] * factor[start]
        # Unpublished days: use the factor of the SAME season a year earlier (a monsoon month's factor
        # applied to a dry month would double output), so present-day disruptions still show through.
        for i, d in enumerate(dts):
            if keys[i][0] > mp.LATEST_PUBLISHED:
                tonnes[i] = df.raw.values[i] * factor.get(mp.period_of(d - timedelta(days=365))[0], 1.0)
        df["tonnes"] = tonnes


def _plans(out, end):
    """Monthly plan = same month a year earlier × PLAN_GROWTH (MOIL's MoU targets aren't public);
    the first year uses its own month × 1.02. Plans run two months past `end`."""
    for mid, df in out.items():
        monthly = df.assign(month=df.date.str[:7]).groupby("month").tonnes.sum()
        months = pd.period_range(HISTORY_START, end + timedelta(days=62), freq="M")
        plan = {}
        for p in months:
            prev = str(p - 12)
            if prev in monthly.index:
                plan[str(p)] = round(monthly[prev] * mp.PLAN_GROWTH, -1)
            elif str(p) in monthly.index and p.start_time.date() >= HISTORY_START and (p.end_time.date() <= end):
                plan[str(p)] = round(monthly[str(p)] * 1.02, -1)
            else:  # future month without a prior-year actual: carry the latest plan
                plan[str(p)] = list(plan.values())[-1] if plan else 0
        df.attrs["plan"] = plan


# ---- records --------------------------------------------------------------------------------
def day_records(out, d, include_production=True):
    recs = []
    for mid, df in out.items():
        if str(d) not in df.index:
            continue
        r = df.loc[str(d)]
        if include_production:
            for (s, _), frac in zip(SHIFTS, r.shares):
                recs.append(dict(type="production", mine_id=mid, date=str(d), shift=s, tonnes=round(float(r.tonnes * frac), 1)))
        recs.append(dict(type="equipment", mine_id=mid, date=str(d), dumpers_total=int(r.dumpers), dumpers_available=int(r.fleet),
                         excavator_availability_pct=round(float(r.avail * 100), 1), downtime_hrs=round(float((1 - r.avail) * 72), 1)))
        recs.append(dict(type="blasting", mine_id=mid, date=str(d), blasts=int(r.blasts), explosive_kg=float(r.explosive)))
        recs.append(dict(type="manpower", mine_id=mid, date=str(d), rostered=int(r.rostered), present=int(r.present)))
    return recs


def plan_records(out):
    return [dict(type="plan", mine_id=mid, month=m, target_t=float(v)) for mid, df in out.items() for m, v in df.attrs["plan"].items()]


def _messy(recs, rng, rate=0.003):
    """Field data is never clean: a few kg-for-tonne slips, negative counts, typo'd mine codes."""
    for r in recs:
        if rng.random() < rate:
            k = rng.integers(3)
            if k == 0 and r["type"] == "production":
                r["tonnes"] = r["tonnes"] * 1000
            elif k == 1:
                r["mine_id"] = r["mine_id"].lower()
            elif r["type"] == "manpower":
                r["present"] = -1
    return recs


# ---- channels -------------------------------------------------------------------------------
def provision():
    keys = {}
    for m in MINES:
        keys[m["id"]] = dict(device_id=f"vm-{m['id'].lower()}-mis", key=ingest.create_device(f"vm-{m['id'].lower()}-mis", m["id"],
                                                                                        f"Virtual mine · {m['name']} shift logger + telematics"))
    KEYS.write_text(json.dumps(keys, indent=2))
    _log(f"provisioned {len(keys)} device keys -> {KEYS.name} (keep this file private)")
    return keys


def _keys():
    if not KEYS.exists():
        return provision()
    return json.loads(KEYS.read_text())


def _post(url, key, recs):
    import requests
    for k in range(4):
        try:
            r = requests.post(f"{url}/api/ingest/telemetry", json=dict(records=recs), headers={"X-Device-Key": key}, timeout=60)
            if r.status_code == 200:
                return r.json()
            _log(f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            _log(f"{e.__class__.__name__}, retry {k + 1}")
        time.sleep(2 * (k + 1))
    raise RuntimeError("ingestion API unreachable")


def backfill(http=None, start=None, end=None, log=_log):
    """Send history start..end (default: everything missing up to yesterday). Returns a summary."""
    end = end or (date.today() - timedelta(days=1))
    if start is None:
        latest = ingest.status()["latest_date"]
        start = date.fromisoformat(latest) + timedelta(days=1) if latest else HISTORY_START
    if start > end:
        log("already up to date")
        return dict(days=0, accepted=0, rejected=0)
    out, real_rain = simulate(end)
    rng = np.random.default_rng(SEED + 11)
    recs = plan_records(out)
    d = start
    while d <= end:
        recs += day_records(out, d)
        d += timedelta(days=1)
    recs = _messy(recs, rng)
    days = (end - start).days + 1
    log(f"backfill {start} -> {end}: {days} days, {len(recs):,} records ({'real ERA5 rain' if real_rain else 'no rain data'})")
    tot = dict(days=days, accepted=0, rejected=0)
    if http:
        keys = _keys()
        for mid in [m["id"] for m in MINES]:
            mine_recs = [r for r in recs if r["mine_id"].upper() == mid]
            for i in range(0, len(mine_recs), 2000):
                s = _post(http, keys[mid]["key"], mine_recs[i:i + 2000])
                tot["accepted"] += s["accepted"]; tot["rejected"] += s["rejected"]
    else:
        for i in range(0, len(recs), 5000):
            s = ingest.accept(recs[i:i + 5000], channel="backfill", source="virtual-mine")
            tot["accepted"] += s["accepted"]; tot["rejected"] += s["rejected"]
    log(f"accepted {tot['accepted']:,}, dead-lettered {tot['rejected']:,}")
    return tot


# ---- live mode ------------------------------------------------------------------------------
def _fleet(m):
    oc = m["method"] == "opencast"
    fleet = [(f"{m['id']}-EX{i}", "excavator") for i in range(1, 4)]
    fleet += [(f"{m['id']}-DM{i:02d}", "dumper") for i in range(1, m["dumpers"] + 1)]
    fleet += [(f"{m['id']}-DR{i}", "drill") for i in range(1, 3)] if oc else \
             [(f"{m['id']}-LD{i}", "loader") for i in range(1, 3)] + [(f"{m['id']}-HS1", "hoist")]
    return fleet


def _shift_now(now):
    h = now.hour
    return "A" if 6 <= h < 14 else ("B" if 14 <= h < 22 else "C")


def live(http, every=20, stop=None, log=None):
    """Heartbeats every `every` seconds; each shift's tonnes when it closes; yesterday's daily logs at 06:05.
    `stop` (threading.Event) ends the loop, so the API can run the feed as a background service."""
    global _log
    if log is not None:
        _log = log
    keys = _keys()
    sent = set()
    _log(f"live -> {http} (heartbeat every {every}s). Ctrl+C to stop.")
    sim_day, out = None, None
    while not (stop and stop.is_set()):
        now = datetime.now()
        today = now.date()
        if sim_day != today:
            out, _ = simulate(today)
            sim_day = today
            backfill(http=http)  # catch up anything missed (idempotent)
        for m in MINES:
            mid = m["id"]
            row = out[mid].loc[str(today)]
            rng = np.random.default_rng(zlib.crc32(f"{today}{mid}".encode()))
            down = set(rng.choice([e for e, _ in _fleet(m)], size=int(round(len(_fleet(m)) * (1 - row.avail))), replace=False)) \
                if row.avail < 1 else set()
            tick = np.random.default_rng(int(now.timestamp()) // every + zlib.crc32(mid.encode()))
            changeover = now.minute < 20 and now.hour in (6, 14, 22)
            hb = []
            for eq, kind in _fleet(m):
                if eq in down:
                    status, util = ("maintenance" if zlib.crc32(eq.encode()) % 3 == 0 else "breakdown"), 0.0
                elif changeover:
                    status, util = "idle", float(tick.uniform(0, 15))
                else:
                    status, util = "working", float(np.clip(tick.normal(72, 12), 20, 99))
                hb.append(dict(type="heartbeat", mine_id=mid, ts=now.isoformat(timespec="seconds"), equipment_id=eq, kind=kind,
                               status=status, utilisation_pct=round(util, 1),
                               payload_t=round(float(tick.uniform(18, 35)), 1) if kind == "dumper" and status == "working" else None,
                               fuel_pct=round(float(tick.uniform(20, 95)), 0) if kind in ("dumper", "excavator", "drill", "loader") else None))
            # a closed shift reports its tonnes once (shift C belongs to the day it started)
            for s, start_h in SHIFTS:
                d = today if s != "C" else today - timedelta(days=1)
                closes = datetime.combine(d, datetime.min.time()) + timedelta(hours=start_h + 8)
                key = (mid, str(d), s)
                if now >= closes and key not in sent:
                    if str(d) in out[mid].index:
                        r = out[mid].loc[str(d)]
                        frac = r.shares[["A", "B", "C"].index(s)]
                        hb.append(dict(type="production", mine_id=mid, date=str(d), shift=s, tonnes=round(float(r.tonnes * frac), 1)))
                    sent.add(key)
            if now.hour >= 6 and (mid, "daily", str(today)) not in sent:
                hb += [r for r in day_records(out, today - timedelta(days=1), include_production=False) if r["mine_id"] == mid]
                sent.add((mid, "daily", str(today)))
            s = _post(http, keys[mid]["key"], hb)
            if s["rejected"]:
                _log(f"{mid}: {s['rejected']} rejected: {s['errors'][:2]}")
        _log(f"{now:%H:%M:%S} heartbeat sent for {len(MINES)} mines (shift {_shift_now(now)})")
        if stop is not None:
            stop.wait(every)
        else:
            time.sleep(every)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["provision", "backfill", "live"])
    ap.add_argument("--http", default=None, help="ingestion API base URL, e.g. http://127.0.0.1:8000")
    ap.add_argument("--every", type=int, default=20)
    a = ap.parse_args()
    if a.mode == "provision":
        provision()
    elif a.mode == "backfill":
        backfill(http=a.http)
    else:
        if not a.http:
            sys.exit("live mode needs --http (it talks to the API like a real device)")
        live(a.http, a.every)
