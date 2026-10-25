"""Layer 1 · Data Sources (SIMULATED feeds).

Real feeds live in kd/eo.py and run first. Any lake file that a real fetch produced (it has a
`<name>.provenance.json` with "real": true) is never overwritten here, and the simulated
production uses the real per-mine rainfall when it is available.

Each function plays the role of one upstream feed in the architecture diagram and writes
what that feed would deliver into the raw data lake, warts and all (cloud gaps, coarse
resolution, duplicate rows, sentinel values, unit errors). Layer 2 has to clean it up.

The feeds are SIMULATED. They're built on a hidden 'true' manganese favourability field so
the models have real structure to learn, including ore horizons with NO known mine on them,
which is what reserve identification should be able to surface. In production, each
function is replaced by a connector (Sentinel Hub / Google Earth Engine, GSI Bhukosh,
NGDR, MOIL MIS, IMD API) that writes the same files.
"""
import json
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import geo
from .config import AS_OF, BOUNDS, FESTIVALS, HISTORY_START, HORIZON_DAYS, LAKE, MINES, RES, SEED

# Ore horizons (lon, lat) traced through the Sausar Group. Stretches beyond the known mines
# (east of Chikla, between Tirodi and Balaghat, and the whole northern horizon) are
# deliberately left unmined.
# Simulated ore horizons, traced through the REAL (OSM-verified) mine positions along the
# ENE-trending Sausar belt. Replaced by real geology in Phase 2.
HORIZONS = [
    ("Ramtek-Tumsar", 1.0, [(79.12, 21.36), (79.27, 21.40), (79.28, 21.42), (79.36, 21.40),
                            (79.50, 21.47), (79.69, 21.55), (79.75, 21.54), (79.92, 21.58), (80.15, 21.63)]),
    ("Tirodi-Balaghat-Ukwa", 1.0, [(79.55, 21.66), (79.65, 21.70), (79.72, 21.68), (79.95, 21.76),
                                   (80.23, 21.85), (80.35, 21.91), (80.47, 21.97)]),
    ("Northern (unexplored)", 0.75, [(79.25, 21.86), (79.42, 21.92), (79.60, 21.98)]),
]
FAULTS = [
    [(79.10, 21.25), (79.60, 21.75)], [(79.70, 21.25), (80.20, 21.95)],
    [(79.40, 22.05), (79.90, 21.55)], [(80.10, 21.40), (80.50, 21.70)],
    [(79.00, 21.65), (79.45, 21.30)],
]
RIVERS = {
    "Wainganga": [(80.05, 22.05), (79.95, 21.85), (79.85, 21.65), (79.72, 21.45), (79.65, 21.25)],
    "Kanhan": [(79.10, 22.05), (79.20, 21.75), (79.30, 21.50), (79.35, 21.25)],
}
GEOLOGY_LEGEND = {0: "Sausar Group (Mn host)", 1: "Tirodi Gneiss", 2: "Sakoli Group",
                  3: "Deccan Trap basalt", 4: "Alluvium"}
STATIONS = [("Balaghat", 21.80, 80.18), ("Bhandara", 21.17, 79.65), ("Nagpur", 21.15, 79.09)]


def has_real(path):
    """True if a real fetch (kd/eo.py) owns this lake file."""
    prov = path.with_name(path.name.split(".")[0] + ".provenance.json")
    return prov.exists() and json.loads(prov.read_text()).get("real", False)


def _save_raster(path, arr, bounds=BOUNDS, res=RES, **meta):
    if has_real(path):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, data=arr.astype(np.float32), bounds=json.dumps(bounds), res=res,
                        meta=json.dumps(meta))


def _truth(rng):
    """Hidden ground truth: favourability F in [0,1], geology units, alluvium mask."""
    lat2d, lon2d = geo.grid()
    fault_d = np.min([geo.dist_km_to_polyline(lat2d, lon2d, f)[0] for f in FAULTS], axis=0)
    F = np.zeros(lat2d.shape)
    hz_d = np.full(lat2d.shape, np.inf)
    for _, strength, line in HORIZONS:
        d, along = geo.dist_km_to_polyline(lat2d, lon2d, line)
        knots = rng.uniform(0.25, 1.0, size=int(along.max() // 4) + 3)
        shoots = np.interp(along, np.arange(len(knots)) * 4.0, knots)  # ore shoots along strike
        F = np.maximum(F, strength * shoots * np.exp(-d ** 2 / (2 * 1.3 ** 2)))
        hz_d = np.minimum(hz_d, d)
    F = np.clip(F * (1 + 0.3 * np.exp(-fault_d / 2.0)), 0, 1)

    river_d = np.min([geo.dist_km_to_polyline(lat2d, lon2d, r)[0] for r in RIVERS.values()], axis=0)
    wobble = geo.smooth_field(lat2d.shape, 6, rng)
    unit = np.ones(lat2d.shape, dtype=int)                                    # Tirodi Gneiss
    unit[(lon2d > 80.0) & (lat2d < 21.7)] = 2                                 # Sakoli Group
    unit[(lon2d < 79.10 + 0.12 * wobble) & (lat2d > 21.55)] = 3               # Deccan Trap
    unit[hz_d < 3.5 + 1.5 * wobble] = 0                                       # Sausar Group
    alluvium = river_d < 0.8 + 0.6 * wobble
    unit[alluvium] = 4
    return dict(lat=lat2d, lon=lon2d, F=F, unit=unit, alluvium=alluvium, fault_d=fault_d)


def satellite(rng, t):
    """Sentinel-2 (Mn ratio index, NDVI), Sentinel-1 VV, SRTM DEM, MODIS LST, IMD/SMAP coarse grids."""
    lat2d, F = t["lat"], t["F"]
    shape = F.shape
    latn = (lat2d - BOUNDS["lat_min"]) / (BOUNDS["lat_max"] - BOUNDS["lat_min"])
    lonn = (t["lon"] - BOUNDS["lon_min"]) / (BOUNDS["lon_max"] - BOUNDS["lon_min"])
    mine_d = geo.dist_km_to_points(lat2d, t["lon"], [(m["lat"], m["lon"]) for m in MINES])
    disturb = np.exp(-mine_d / 0.8) * (mine_d < 2.0)  # open pits, dumps, haul roads
    allu = t["alluvium"].astype(float)

    forest = 0.30 + 0.45 * geo.smooth_field(shape, 12, rng) + 0.15 * latn
    ndvi = forest - 0.12 * F - 0.20 * allu - 0.35 * disturb + 0.03 * rng.standard_normal(shape)
    ndvi = np.clip(ndvi, -0.1, 0.9)

    elev = 280 + 250 * geo.smooth_field(shape, 20, rng) + 120 * latn + 45 * F
    elev += 60 * (t["unit"] == 3) - 25 * allu
    gy, gx = np.gradient(elev, RES * geo.KM_PER_DEG_LAT * 1000, RES * 105_000)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))

    mn = (0.25 + 0.5 * F * (1 - 0.6 * np.clip(ndvi, 0, 1)) * np.where(allu > 0, 0.3, 1.0)
          + 0.08 * geo.smooth_field(shape, 4, rng) + 0.2 * disturb + 0.04 * rng.standard_normal(shape))
    sar = (-13 + 4 * slope / (slope.max() + 1e-9) + 1.2 * F + 1.5 * forest
           + 0.6 * rng.standard_normal(shape))
    lst = 36 - 0.007 * (elev - 300) - 7 * ndvi + 1.2 * F + 0.5 * rng.standard_normal(shape)

    clouds = geo.smooth_field(shape, 3, rng) > 0.80  # optical/thermal gaps
    for arr in (mn, ndvi, lst):
        arr[clouds] = np.nan

    sat = LAKE / "satellite"
    _save_raster(sat / "sentinel2_mn_ratio.npz", mn, sensor="Sentinel-2 MSI", product="SWIR/NIR Mn-oxide ratio, Oct-Mar median")
    _save_raster(sat / "sentinel2_ndvi.npz", ndvi, sensor="Sentinel-2 MSI", product="NDVI, Oct-Mar median")
    _save_raster(sat / "sentinel1_vv.npz", sar, sensor="Sentinel-1 C-SAR", product="VV backscatter dB")
    _save_raster(sat / "srtm_dem.npz", elev, sensor="SRTM 1-arcsec", product="elevation m")
    _save_raster(sat / "modis_lst.npz", lst, sensor="MODIS MOD11A2", product="day LST degC")

    # Coarse 0.25 deg grids (IMD gridded rainfall, SMAP soil moisture) over a padded extent.
    cb = dict(lat_min=21.00, lat_max=22.25, lon_min=78.75, lon_max=80.75)
    clat = np.arange(cb["lat_max"] - 0.125, cb["lat_min"], -0.25)[:, None]
    clon = np.arange(cb["lon_min"] + 0.125, cb["lon_max"], 0.25)[None, :]
    rain = 1050 + 450 * (clat - 21) / 1.25 + 150 * (clon - 78.75) / 2 + 40 * rng.standard_normal((len(clat), clon.shape[1]))
    sm = 0.16 + 0.10 * (clat - 21) / 1.25 + 0.02 * rng.standard_normal(rain.shape)
    _save_raster(sat / "imd_rain_annual.npz", rain, bounds=cb, res=0.25, sensor="IMD 0.25deg gridded", product="annual rainfall mm")
    _save_raster(sat / "smap_soil_moisture.npz", sm, bounds=cb, res=0.25, sensor="SMAP L3", product="soil moisture m3/m3")
    return dict(mn=mn, ndvi=ndvi, lst=lst)


def geology(rng, t):
    """GSI geological map, lineaments, boreholes, stream-sediment geochemistry, aeromagnetics."""
    lat2d, lon2d, F = t["lat"], t["lon"], t["F"]
    g = LAKE / "geology"
    _save_raster(g / "gsi_geology_units.npz", t["unit"].astype(float), source="GSI Bhukosh 1:50k (simulated)",
                 legend=GEOLOGY_LEGEND)
    feats = [{"type": "Feature", "properties": {"kind": "fault", "id": f"L{i+1}"},
              "geometry": {"type": "LineString", "coordinates": line}} for i, line in enumerate(FAULTS)]
    feats += [{"type": "Feature", "properties": {"kind": "river", "id": name},
               "geometry": {"type": "LineString", "coordinates": line}} for name, line in RIVERS.items()]
    (g / "lineaments.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))

    def f_at(la, lo):
        rc = geo.cell_of(la, lo)
        return F[rc] if rc else 0.0

    # Boreholes: most cluster around known deposits, some are regional exploration holes.
    from .config import KNOWN_DEPOSITS
    rows = []
    for i in range(260):
        if i < 180:
            _, la, lo, _src = KNOWN_DEPOSITS[rng.integers(len(KNOWN_DEPOSITS))]
            la += rng.normal(0, 0.018); lo += rng.normal(0, 0.018)
        else:
            la = rng.uniform(BOUNDS["lat_min"], BOUNDS["lat_max"])
            lo = rng.uniform(BOUNDS["lon_min"], BOUNDS["lon_max"])
        la = float(np.clip(la, BOUNDS["lat_min"] + 0.005, BOUNDS["lat_max"] - 0.005))
        lo = float(np.clip(lo, BOUNDS["lon_min"] + 0.005, BOUNDS["lon_max"] - 0.005))
        f = f_at(la, lo) + rng.normal(0, 0.08)
        hit = f > 0.35
        rows.append(dict(
            hole_id=f"BH-{i+1:04d}", lat=round(la, 5), lon=round(lo, 5),
            drilled_on=str(date(2005, 1, 1) + timedelta(days=int(rng.integers(0, 7000)))),
            hole_depth_m=round(float(rng.uniform(80, 320)), 1),
            depth_to_ore_m=round(float(rng.uniform(20, 180)), 1) if hit else None,
            thickness_m=round(float(np.clip(2 + 10 * f + rng.normal(0, 1.2), 0.8, 16)), 2) if hit else 0.0,
            grade_mn_pct=round(float(np.clip(18 + 22 * f + rng.normal(0, 3), 10, 48)), 2) if hit
            else round(float(rng.uniform(1.5, 8)), 2),
        ))
    pd.DataFrame(rows).to_csv(g / "boreholes.csv", index=False)

    # Stream sediment Mn (dispersion trains: a blurred version of F).
    blur = np.real(np.fft.ifft2(np.fft.fft2(F) * np.exp(-2 * (np.pi * 3) ** 2 * (
        np.fft.fftfreq(F.shape[0])[:, None] ** 2 + np.fft.fftfreq(F.shape[1])[None, :] ** 2))))
    blur = blur / blur.max()
    samples = []
    for i in range(450):
        la = rng.uniform(BOUNDS["lat_min"], BOUNDS["lat_max"]); lo = rng.uniform(BOUNDS["lon_min"], BOUNDS["lon_max"])
        rc = geo.cell_of(la, lo)
        ppm = float(np.exp(np.log(700) + 2.2 * blur[rc] + rng.normal(0, 0.35)))
        samples.append(dict(sample_id=f"SS-{i+1:04d}", lat=round(la, 5), lon=round(lo, 5), mn_ppm=round(ppm, 1)))
    pd.DataFrame(samples).to_csv(g / "geochem_stream_sediment.csv", index=False)

    # Aeromagnetic survey: E-W flight lines every 0.04 deg, a reading every 0.005 deg.
    regional = geo.smooth_field(F.shape, 25, rng)
    pts = []
    for la in np.arange(BOUNDS["lat_min"] + 0.01, BOUNDS["lat_max"], 0.04):
        for lo in np.arange(BOUNDS["lon_min"], BOUNDS["lon_max"], 0.005):
            rc = geo.cell_of(la, lo)
            if rc:
                pts.append((round(la, 4), round(lo, 4), round(float(60 * regional[rc] + 90 * F[rc] + rng.normal(0, 8)), 2)))
    pd.DataFrame(pts, columns=["lat", "lon", "mag_nT"]).to_csv(g / "aeromag_flightlines.csv", index=False)


def _daily_rain(rng, days, dist_factor):
    """IMD-like daily rainfall with a June-September monsoon."""
    out = np.zeros(len(days))
    wet = False
    for i, d in enumerate(days):
        doy = d.timetuple().tm_yday
        monsoon = np.exp(-((doy - 205) / 38) ** 2)  # peaks late July
        p_wet = 0.04 + 0.70 * monsoon
        p_wet = min(0.95, p_wet + (0.15 if wet else 0))
        wet = rng.random() < p_wet
        if wet:
            out[i] = rng.gamma(0.9, 8 + 22 * monsoon) * dist_factor
    return out


def production_and_external(rng):
    """Fallback weather + forecast (only when the real ERA5/Open-Meteo feeds are unavailable)
    and the regulatory limits table."""
    days = [HISTORY_START + timedelta(days=i) for i in range((AS_OF - HISTORY_START).days)]
    n = len(days)
    dts = pd.to_datetime(days)
    years = np.array([(d - HISTORY_START).days / 365.25 for d in days])

    # Weather: a regional monsoon signal shared by stations plus local variation.
    regional = _daily_rain(rng, days, 1.0)
    station_rain = {}
    w = LAKE / "external"; w.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, la, lo in STATIONS:
        local = regional * rng.lognormal(0, 0.35, n) * (1.15 if name == "Balaghat" else 1.0)
        local += (rng.random(n) < 0.03) * rng.gamma(1, 6, n)
        local[(dts.year == 2026) & (dts.month == 10) & (dts.day.isin([3, 4]))] += 22  # late-monsoon showers
        station_rain[name] = local
        rows += [dict(station=name, lat=la, lon=lo, date=str(d), rain_mm=round(float(r), 1)) for d, r in zip(days, local)]
    weather = pd.DataFrame(rows)
    weather.loc[weather.sample(frac=0.004, random_state=SEED).index, "rain_mm"] = -999.0  # station sentinel
    real_rain = {}
    if has_real(w / "imd_station_daily_rain.csv"):
        # Real ERA5 rain per mine (kd/eo.py) drives the simulated operations instead.
        rw = pd.read_csv(w / "imd_station_daily_rain.csv")
        for mid, g in rw.groupby("station"):
            real_rain[mid] = g.set_index("date").rain_mm.reindex([str(d) for d in days]).fillna(0).values
    else:
        weather.to_csv(w / "imd_station_daily_rain.csv", index=False)

    # 30-day extended-range forecast from AS_OF, with a low-pressure system around day 6-9.
    fc = []
    spell = {6: 55, 7: 80, 8: 38, 9: 12}
    for name, la, lo in STATIONS:
        for k in range(HORIZON_DAYS):
            d = AS_OF + timedelta(days=k)
            base = max(0.0, rng.gamma(0.5, 3) - 1.0) if rng.random() < 0.15 else 0.0
            r = spell.get(k, base) * (0.8 if name == "Nagpur" else 1.0)
            fc.append(dict(station=name, issued=str(AS_OF), date=str(d), rain_mm=round(float(r), 1),
                           p_heavy=round(min(0.95, r / 70), 2)))
    if not has_real(w / "imd_extended_range_forecast.csv"):
        pd.DataFrame(fc).to_csv(w / "imd_extended_range_forecast.csv", index=False)

    reg = [dict(mine_id=m["id"], ec_cap_tpa=m["ec_cap_tpa"], ec_valid_till="2031-03-31",
                monsoon_blast_limit_per_day=1 if m["method"] == "opencast" else None,
                monsoon_window="06-15/09-15") for m in MINES]
    pd.DataFrame(reg).to_csv(w / "regulatory_limits.csv", index=False)

    # Mine operations (production, equipment, blasting, manpower, plan) are no longer generated
    # here: they arrive through the ingestion API (kd/ingest.py), fed by kd/virtual_mine.py.


def generate_all():
    rng = np.random.default_rng(SEED)
    t = _truth(rng)
    satellite(rng, t)
    geology(rng, t)
    production_and_external(rng)
    # Kept outside the lake: used only by the model card to check recovery of unmined horizons.
    np.save(LAKE.parent / "_synthetic_truth_F.npy", t["F"].astype(np.float32))
