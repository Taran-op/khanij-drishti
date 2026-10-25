"""Layer 2 · Data Ingestion & Processing.

ingest -> preprocess (clean, standardise, fill gaps, align) -> geospatial processing
-> integration (spatio-temporal joins) -> feature engineering.
"""
import json

import numpy as np
import pandas as pd
from scipy import ndimage

from . import geo, storage
from .config import FESTIVALS, LAKE, MINES, RES

# Where each raw feed comes from in a real deployment (shown on the System page).
CHANNELS = {
    "satellite": "API · Copernicus / Earth Engine / IMD gridded",
    "geology": "Manual upload · GSI Bhukosh, NGDR, drilling contractor logs",
    "production": "IoT + MIS export · fleet telemetry, shift reports, blasting register",
    "external": "API · IMD station + extended-range forecast, MoEFCC/IBM limits",
}


# ---- ingestion -------------------------------------------------------------------------------
def _raster(path):
    z = np.load(path, allow_pickle=False)
    return z["data"].astype(float), json.loads(str(z["bounds"])), float(z["res"]), json.loads(str(z["meta"]))


def ingest():
    raw = {"rasters": {}, "tables": {}, "provenance": {}}
    log = []
    # sidecars first: "x.npz" sorts before "x.provenance.json", and each file's log row needs its provenance
    for p in LAKE.rglob("*.provenance.json"):
        raw["provenance"][p.name.split(".")[0]] = json.loads(p.read_text())
    for p in sorted(LAKE.rglob("*")):
        if p.name.endswith(".provenance.json") or p.parent.name == "uploads":
            continue  # uploads are applied separately (kd/uploads.py), not ingested raw
        if p.suffix == ".npz":
            raw["rasters"][p.stem] = _raster(p)
            n = raw["rasters"][p.stem][0].size
        elif p.suffix == ".csv":
            raw["tables"][p.stem] = pd.read_csv(p)
            n = len(raw["tables"][p.stem])
        elif p.suffix == ".geojson":
            raw[p.stem] = json.loads(p.read_text())
            n = len(raw[p.stem]["features"])
        else:
            continue
        prov = raw["provenance"].get(p.name.split(".")[0])
        log.append(dict(source=p.parent.name, file=p.name, channel=CHANNELS.get(p.parent.name, ""), records=n,
                        real=bool(prov and prov.get("real")), origin=(prov or {}).get("source", "KhanijDrishti simulator")))
    storage.write_table("ingestion_log", pd.DataFrame(log))
    storage.write_table("provenance", pd.DataFrame([
        dict(name=k, real=v.get("real", False), source=v.get("source"), collection=v.get("collection"),
             licence=v.get("licence"), date_range=v.get("date_range") or v.get("year"), method=v.get("method"),
             scenes=len(v.get("scenes", [])), fetched_at=v.get("fetched_at"))
        for k, v in raw["provenance"].items()]))
    return raw


# ---- raster preprocessing --------------------------------------------------------------------
def fill_gaps(arr, max_iter=200):
    """Fill NaNs by repeated 3x3 neighbourhood averaging (simple inpainting)."""
    a = arr.copy()
    for _ in range(max_iter):
        nan = np.isnan(a)
        if not nan.any():
            break
        vals = np.where(nan, 0, a)
        w = (~nan).astype(float)
        s = ndimage.uniform_filter(vals, 3, mode="nearest")
        ws = ndimage.uniform_filter(w, 3, mode="nearest")
        a = np.where(nan & (ws > 0), s / np.maximum(ws, 1e-9), a)
    return a


def preprocess_rasters(raw):
    r = raw["rasters"]
    report = {}
    out = {}
    for key, name in [("sentinel2_mn_ratio", "mn_index"), ("sentinel2_ndvi", "ndvi"),
                      ("sentinel1_vv", "sar_vv"), ("srtm_dem", "elevation"), ("modis_lst", "lst")]:
        arr = r[key][0]
        report[name] = dict(gap_pct=round(float(np.isnan(arr).mean() * 100), 2))
        out[name] = fill_gaps(arr)
    # Spatial alignment: coarse 0.25 deg products resampled to the 0.01 deg analysis grid.
    for key, name in [("imd_rain_annual", "rain_annual"), ("smap_soil_moisture", "soil_moisture")]:
        arr, b, res, _ = r[key]
        out[name] = geo.resample_coarse(arr, b, res)
        report[name] = dict(resampled_from_deg=res)
    # Extra real-only layers (present when kd/eo.py fetched them).
    for key, name in [("sentinel2_iron_ratio", "iron_ratio"), ("sentinel2_clay_ratio", "clay_ratio"), ("sentinel1_vh", "sar_vh")]:
        if key in r:
            arr = r[key][0]
            report[name] = dict(gap_pct=round(float(np.isnan(arr).mean() * 100), 2))
            out[name] = fill_gaps(arr)
    if "macrostrat_units" in r:
        arr, _, _, meta = r["macrostrat_units"]
        out["geology_unit"] = np.round(fill_gaps(arr)).astype(int)
        out["_geology_legend"] = {int(k): v for k, v in meta["legend"].items()}
        report["geology_unit"] = dict(source="Macrostrat", units=len(meta["legend"]))
    else:
        from .sources import GEOLOGY_LEGEND
        out["geology_unit"] = r["gsi_geology_units"][0].round().astype(int)
        out["_geology_legend"] = dict(GEOLOGY_LEGEND)
    return out, report


def geospatial(layers, raw):
    """Derived GIS layers + spatial-context features."""
    lat2d, lon2d = geo.grid()
    L = dict(layers)

    # Leakage guard: active pits/dumps look like 'ore' to any optical model. Mask 2 km around
    # producing mines and inpaint from the surrounding background so the model can't cheat.
    mine_d = geo.dist_km_to_points(lat2d, lon2d, [(m["lat"], m["lon"]) for m in MINES])
    guard = mine_d < 2.0
    for k in ("mn_index", "ndvi", "lst", "sar_vv", "iron_ratio", "clay_ratio", "sar_vh"):
        if k in L:
            a = L[k].copy(); a[guard] = np.nan; L[k] = fill_gaps(a)

    gy, gx = np.gradient(L["elevation"], RES * geo.KM_PER_DEG_LAT * 1000, RES * 105_000)
    L["slope"] = np.degrees(np.arctan(np.hypot(gx, gy)))
    az, alt = np.radians(315), np.radians(45)
    asp = np.arctan2(-gx, gy)
    sl = np.arctan(np.hypot(gx, gy) * 4)
    L["hillshade"] = np.clip(np.sin(alt) * np.cos(sl) + np.cos(alt) * np.sin(sl) * np.cos(az - asp), 0, 1)

    # Spatial context (what a CNN's receptive field sees): neighbourhood stats of the Mn index.
    for size in (3, 7):
        m = ndimage.uniform_filter(L["mn_index"], size, mode="nearest")
        L[f"mn_ctx{size}_mean"] = m
    m2 = ndimage.uniform_filter(L["mn_index"] ** 2, 7, mode="nearest")
    L["mn_ctx7_std"] = np.sqrt(np.maximum(m2 - L["mn_ctx7_mean"] ** 2, 0))

    if "dem_lineament_km" in raw["rasters"]:
        L["lineament_km"] = fill_gaps(raw["rasters"]["dem_lineament_km"][0])
    else:
        faults = [f["geometry"]["coordinates"] for f in raw["lineaments"]["features"] if f["properties"]["kind"] == "fault"]
        L["lineament_km"] = np.min([geo.dist_km_to_polyline(lat2d, lon2d, f)[0] for f in faults], axis=0)

    # Geochemistry: inverse-distance-weighted log(Mn ppm) from stream-sediment samples.
    s = raw["tables"]["geochem_stream_sediment"]
    dy = (lat2d.ravel()[:, None] - s.lat.values[None, :]) * geo.KM_PER_DEG_LAT
    dx = (lon2d.ravel()[:, None] - s.lon.values[None, :]) * geo.km_per_deg_lon(21.65)
    w = 1 / (dx ** 2 + dy ** 2 + 0.25)
    L["geochem_mn_ppm"] = np.exp((w @ np.log(s.mn_ppm.values)) / w.sum(1)).reshape(lat2d.shape)

    # Aeromagnetics: bin flight-line readings into cells, then inpaint between lines.
    mg = raw["tables"]["aeromag_flightlines"]
    rows, cols = lat2d.shape
    rr = ((lat2d.max() + RES / 2 - mg.lat) / RES).astype(int).clip(0, rows - 1)
    cc = ((mg.lon - (lon2d.min() - RES / 2)) / RES).astype(int).clip(0, cols - 1)
    acc = np.zeros((rows, cols)); cnt = np.zeros((rows, cols))
    np.add.at(acc, (rr, cc), mg.mag_nT.values); np.add.at(cnt, (rr, cc), 1)
    L["mag_nT"] = fill_gaps(np.where(cnt > 0, acc / np.maximum(cnt, 1), np.nan))
    if "emag2_mag" in raw["rasters"]:
        L["mag_nT"] = fill_gaps(raw["rasters"]["emag2_mag"][0])

    for code in sorted(L["_geology_legend"]):
        L[f"geo_u{code}"] = (L["geology_unit"] == code).astype(float)
    return L


RESERVE_FEATURES = ["mn_index", "mn_ctx3_mean", "mn_ctx7_mean", "mn_ctx7_std", "ndvi", "lst", "sar_vv",
                    "elevation", "slope", "lineament_km", "geochem_mn_ppm", "mag_nT",
]


OPTIONAL_FEATURES = ["iron_ratio", "clay_ratio", "sar_vh"]


def reserve_feature_table(L):
    lat2d, lon2d = geo.grid()
    geo_cols = sorted(k for k in L if k.startswith("geo_u"))
    df = pd.DataFrame({k: L[k].ravel() for k in RESERVE_FEATURES + [f for f in OPTIONAL_FEATURES if f in L] + geo_cols})
    df["geochem_mn_ppm"] = np.log10(df["geochem_mn_ppm"])
    df = df.rename(columns={"geochem_mn_ppm": "geochem_log_mn"})
    df.insert(0, "lon", lon2d.ravel().round(4)); df.insert(0, "lat", lat2d.ravel().round(4))
    df.insert(0, "cell_id", np.arange(len(df)))
    return df


# ---- tabular preprocessing + integration -----------------------------------------------------
def clean_production(raw):
    t = raw["tables"]
    rep = {}
    p = t["mis_shift_production"].copy()
    n0 = len(p)
    p = p.drop_duplicates(["date", "mine_id", "shift"])
    rep["duplicates_dropped"] = n0 - len(p)
    rep["sentinels_to_null"] = int((p.tonnes < 0).sum())
    p.loc[p.tonnes < 0, "tonnes"] = np.nan
    med = p.groupby(["mine_id", "shift"]).tonnes.transform("median")
    unit_err = p.tonnes > 50 * med
    rep["kg_to_tonne_fixes"] = int(unit_err.sum())
    p.loc[unit_err, "tonnes"] = p.loc[unit_err, "tonnes"] / 1000
    # Shifts rejected at ingestion (dead-lettered) are absent: make them explicit gaps, then impute.
    grid = pd.MultiIndex.from_product([sorted(p.date.unique()), sorted(p.mine_id.unique()), ["A", "B", "C"]],
                                      names=["date", "mine_id", "shift"])
    p = p.set_index(["date", "mine_id", "shift"]).reindex(grid).reset_index()
    rep["missing_imputed"] = int(p.tonnes.isna().sum())
    p = p.sort_values(["mine_id", "shift", "date"])
    p["tonnes"] = p.groupby(["mine_id", "shift"]).tonnes.transform(lambda s: s.interpolate(limit_direction="both"))
    daily = p.groupby(["date", "mine_id"], as_index=False).tonnes.sum().rename(columns={"tonnes": "actual_t"})

    w = t["imd_station_daily_rain"].copy()
    rep["weather_sentinels"] = int((w.rain_mm < 0).sum())
    w.loc[w.rain_mm < 0, "rain_mm"] = np.nan
    w["rain_mm"] = w.rain_mm.fillna(w.groupby("date").rain_mm.transform("mean")).fillna(0)
    return daily, w, rep


def nearest_station(lat, lon, stations):
    return min(stations, key=lambda s: (s[1] - lat) ** 2 + (s[2] - lon) ** 2)[0]


def integrate(raw, daily, weather):
    """Spatio-temporal join of production, equipment, blasting, manpower, plan and weather."""
    t = raw["tables"]
    stations = weather[["station", "lat", "lon"]].drop_duplicates().values.tolist()
    mines = pd.DataFrame(MINES)
    mines["station"] = [nearest_station(a, b, stations) for a, b in zip(mines.lat, mines.lon)]

    df = daily.merge(t["equipment_telemetry"], on=["date", "mine_id"], how="left")
    df = df.merge(t["blasting_register"], on=["date", "mine_id"], how="left")
    df = df.merge(t["manpower_attendance"], on=["date", "mine_id"], how="left")
    df = df.merge(mines[["id", "method", "capacity_tpd", "station"]].rename(columns={"id": "mine_id"}), on="mine_id")
    df = df.merge(weather[["station", "date", "rain_mm"]], on=["station", "date"], how="left")

    plan = t["monthly_plan"].copy()
    df["month"] = df.date.str[:7]
    df = df.merge(plan, on=["month", "mine_id"], how="left")
    df["target_t"] = df.target_t / pd.to_datetime(df.date).dt.days_in_month
    df = df.sort_values(["mine_id", "date"]).reset_index(drop=True)
    df = add_weather_memory(df)
    df["excavator_avail"] = df.excavator_availability_pct / 100
    df["fleet_ratio"] = df.dumpers_available / df.dumpers_total
    df["manpower_ratio"] = df.present / df.rostered
    df["festival"] = df.date.isin(FESTIVALS).astype(int)
    keep = ["date", "mine_id", "method", "capacity_tpd", "station", "actual_t", "target_t", "rain_mm", "rain_prev",
            "api", "excavator_avail", "fleet_ratio", "manpower_ratio", "blasts", "explosive_kg", "downtime_hrs",
            "dumpers_available", "dumpers_total", "festival"]
    # Feeds close their day at different times (shift logs arrive live, daily equipment / attendance
    # summaries and reanalysis rain lag). Trailing days where any mine still lacks a driver are not
    # complete yet: they are left out until the missing feed arrives.
    core = ["rain_mm", "excavator_avail", "fleet_ratio", "manpower_ratio", "blasts"]
    complete = df.groupby("date")[core].apply(lambda g: g.notna().all().all())
    done = complete[complete].index.max()
    return df.loc[df.date <= done, keep].reset_index(drop=True), mines


def add_weather_memory(df):
    """Previous-day rain and the antecedent precipitation index (soil wetness proxy)."""
    df = df.copy()
    df["rain_prev"] = df.groupby("mine_id").rain_mm.shift(1).fillna(0)
    api = np.zeros(len(df))
    for _, idx in df.groupby("mine_id").indices.items():
        prev = 0.0
        for i in idx:
            prev = prev * 0.85 + df.rain_mm.iat[i]
            api[i] = prev
    df["api"] = api
    return df


MINE_CODE = {m["id"]: i for i, m in enumerate(MINES)}
PRODUCTION_FEATURES = ["mine_code", "is_opencast", "doy_sin", "doy_cos", "t_years", "rain_mm", "rain_prev", "api",
                       "excavator_avail", "fleet_ratio", "manpower_ratio", "blasts", "festival"]


def production_features(df):
    """Same function for training rows and future scenario rows (no train/serve skew)."""
    d = pd.to_datetime(df.date)
    doy = d.dt.dayofyear
    X = pd.DataFrame(dict(
        mine_code=df.mine_id.map(MINE_CODE), is_opencast=(df.method == "opencast").astype(int),
        doy_sin=np.sin(2 * np.pi * doy / 365.25), doy_cos=np.cos(2 * np.pi * doy / 365.25),
        t_years=(d - pd.Timestamp("2023-01-01")).dt.days / 365.25,
        rain_mm=df.rain_mm, rain_prev=df.rain_prev, api=df.api, excavator_avail=df.excavator_avail,
        fleet_ratio=df.fleet_ratio, manpower_ratio=df.manpower_ratio, blasts=df.blasts, festival=df.festival,
    ), index=df.index)
    return X[PRODUCTION_FEATURES]


def run():
    raw = ingest()
    from .uploads import apply_uploads
    applied = apply_uploads(raw)  # accepted manual uploads override open-data / simulated equivalents
    if applied:
        storage.write_table("provenance", pd.DataFrame([
            dict(name=k, real=v.get("real", False), source=v.get("source"), collection=v.get("collection"),
                 licence=v.get("licence"), date_range=v.get("date_range") or v.get("year"), method=v.get("method"),
                 scenes=len(v.get("scenes", [])), fetched_at=v.get("fetched_at"))
            for k, v in raw["provenance"].items()]))
    storage.log_run(2, "ingestion", files=sum(len(v) for v in (raw["rasters"], raw["tables"])) + 1, uploads_applied=applied)

    layers, rep = preprocess_rasters(raw)
    storage.log_run(2, "raster preprocessing", **rep)
    L = geospatial(layers, raw)
    storage.log_run(2, "geospatial processing", layers=len(L))

    daily, weather, crep = clean_production(raw)
    storage.log_run(2, "tabular cleaning", **crep)
    unified, mines = integrate(raw, daily, weather)
    storage.log_run(2, "data integration", rows=len(unified), mines=len(mines))

    # Layer 3 writes
    storage.write_table("production_daily", unified)
    storage.write_table("mines", mines)
    storage.write_table("weather_daily", weather)
    storage.write_table("weather_forecast", raw["tables"]["imd_extended_range_forecast"])
    storage.write_table("regulatory_limits", raw["tables"]["regulatory_limits"])
    storage.write_table("monthly_plan", raw["tables"]["monthly_plan"])
    storage.write_table("boreholes", raw["tables"]["boreholes"])
    storage.write_table("geochem_samples", raw["tables"]["geochem_stream_sediment"])

    # layer → (lake file it comes from, unit, group, description). Provenance is looked up from
    # the file's sidecar, so every map layer says whether it is REAL or SIMULATED and from where.
    spec = {
        "mn_index": ("sentinel2_mn_ratio", "index", "Satellite", "Fe–Mn oxide index (Sentinel-2)"),
        "iron_ratio": ("sentinel2_iron_ratio", "ratio", "Satellite", "Ferric-oxide ratio B4/B2 (Sentinel-2)"),
        "clay_ratio": ("sentinel2_clay_ratio", "ratio", "Satellite", "Clay/hydroxyl ratio B11/B12 (Sentinel-2)"),
        "ndvi": ("sentinel2_ndvi", "", "Satellite", "Vegetation index NDVI (Sentinel-2)"),
        "lst": ("modis_lst", "°C", "Satellite", "Land surface temperature (MODIS)"),
        "sar_vv": ("sentinel1_vv", "dB", "Satellite", "SAR VV backscatter (Sentinel-1)"),
        "sar_vh": ("sentinel1_vh", "dB", "Satellite", "SAR VH backscatter (Sentinel-1)"),
        "elevation": ("srtm_dem", "m", "Terrain", "Elevation (Copernicus DEM)"),
        "slope": ("srtm_dem", "°", "Terrain", "Slope"),
        "hillshade": ("srtm_dem", "", "Terrain", "Hillshade"),
        "rain_annual": ("imd_rain_annual", "mm", "Climate", "Annual rainfall"),
        "soil_moisture": ("smap_soil_moisture", "m³/m³", "Climate", "Soil moisture"),
        "geology_unit": ("macrostrat_units" if "macrostrat_units" in raw["rasters"] else "gsi_geology_units", "class", "Geology", "Lithology"),
        "geochem_mn_ppm": ("geochem_stream_sediment", "ppm", "Geology", "Stream-sediment Mn (IDW)"),
        "mag_nT": ("emag2_mag" if "emag2_mag" in raw["rasters"] else "aeromag_flightlines", "nT", "Geophysics", "Magnetic anomaly"),
        "lineament_km": ("dem_lineament_km" if "dem_lineament_km" in raw["rasters"] else "lineaments", "km", "Geology",
                         "Distance to DEM lineament" if "dem_lineament_km" in raw["rasters"] else "Distance to mapped fault"),
    }
    cat = {}
    for name, (src, unit, group, desc) in spec.items():
        if name not in L:
            continue
        prov = raw["provenance"].get(src, {})
        real = bool(prov.get("real"))
        cat[name] = dict(unit=unit, group=group, desc=desc, real=real,
                         source=prov.get("source", "KhanijDrishti simulator") + (f" · {prov['collection']}" if prov.get("collection") else ""),
                         date_range=str(prov.get("date_range") or prov.get("year") or ""))
    storage.write_grid({k: L[k] for k in cat}, cat)
    storage.write_vector("lineaments", raw.get("osm_rivers") or raw["lineaments"])
    storage.write_table("geology_legend", pd.DataFrame(
        [dict(code=k, name=v) for k, v in sorted(L["_geology_legend"].items())]))

    feats = reserve_feature_table(L)
    storage.fs_put("reserve_features", feats, "Per-cell features for prospectivity mapping (0.01° grid)")
    storage.fs_put("production_features", pd.concat([unified[["date", "mine_id", "actual_t", "target_t", "capacity_tpd"]],
                                                     production_features(unified)], axis=1),
                   "Per mine-day driver features for production forecasting")
    storage.log_run(3, "storage", warehouse_rows=len(unified), grid_cells=feats.shape[0])
    return dict(raw=raw, layers=L, unified=unified)


if __name__ == "__main__":
    run()
