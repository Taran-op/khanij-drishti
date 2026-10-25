"""Layer 1 · REAL Earth-observation and weather sources.

Everything here downloads genuine data and writes it into the data lake with a provenance
sidecar (source, collection, scene ids, dates, licence, fetch time):

  Sentinel-2 L2A    Microsoft Planetary Computer  cloud-masked median composite → spectral indices
  Sentinel-1 RTC    Microsoft Planetary Computer  VV / VH backscatter (dB), median
  Copernicus DEM    Microsoft Planetary Computer  GLO-30 elevation
  MODIS MOD11A2     Microsoft Planetary Computer  8-day day-time land-surface temperature
  ERA5 / ERA5-Land  Open-Meteo archive            daily rain + soil moisture per mine, gridded climatology
  Forecast          Open-Meteo forecast           16-day daily rain per mine

Rasters are read straight onto the 0.01° analysis grid. COG overviews mean only a few MB move.
Each fetch is cached in the lake; pass refresh=True (or `python -m kd.eo --refresh`) to redo it.
"""
import json
import sys
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import requests

from . import geo
from .config import AS_OF, BOUNDS, HISTORY_START, HORIZON_DAYS, LAKE, MINES, RES

PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
BBOX = [BOUNDS["lon_min"], BOUNDS["lat_min"], BOUNDS["lon_max"], BOUNDS["lat_max"]]
SAT = LAKE / "satellite"
EXT = LAKE / "external"

# Dry season after the monsoon: least cloud, crops harvested, rock and soil most exposed.
DRY_SEASON = (date(AS_OF.year - 1 if AS_OF.month < 11 else AS_OF.year, 11, 1),
              date(AS_OF.year if AS_OF.month < 11 else AS_OF.year + 1, 3, 31))


def _log(msg):
    print(f"[eo] {msg}", flush=True)


def _save_raster(name, arr, provenance, **meta):
    SAT.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(SAT / f"{name}.npz", data=arr.astype(np.float32), bounds=json.dumps(BOUNDS), res=RES,
                        meta=json.dumps(meta))
    (SAT / f"{name}.provenance.json").write_text(json.dumps(
        dict(name=name, real=True, fetched_at=datetime.now().isoformat(timespec="seconds"), **provenance), indent=2))


def _fresh(name):
    """Fetched before (a provenance sidecar exists). Simulated files in the lake don't count."""
    return (SAT / f"{name}.npz").exists() and (SAT / f"{name}.provenance.json").exists()


def _catalog():
    import planetary_computer
    import pystac_client
    return pystac_client.Client.open(PC_STAC, modifier=planetary_computer.sign_inplace)


def _to_grid(da):
    """Nearest-sample an (lat, lon) DataArray onto the analysis-grid cell centres (north-up)."""
    lat2d, lon2d = geo.grid()
    ycoord = "latitude" if "latitude" in da.dims else "y"
    xcoord = "longitude" if "longitude" in da.dims else "x"
    out = da.sel({ycoord: lat2d[:, 0], xcoord: lon2d[0, :]}, method="nearest")
    return np.asarray(out.values, dtype=float)


def _load(items, bands, resolution=RES, resampling="average"):
    import odc.stac
    return odc.stac.load(items, bands=bands, crs="EPSG:4326", resolution=resolution, bbox=BBOX,
                         groupby="solar_day", resampling=resampling, fail_on_error=False)


def _pick(items, per_tile=4, tile_key="s2:mgrs_tile"):
    """Keep the least-cloudy few scenes per tile: full coverage at a fraction of the reads."""
    by = {}
    for it in items:
        by.setdefault(it.properties.get(tile_key, "?"), []).append(it)
    out = []
    for group in by.values():
        group.sort(key=lambda i: i.properties.get("eo:cloud_cover", 0))
        out += group[:per_tile]
    return out


# ---- Sentinel-2 -----------------------------------------------------------------------------
S2_BANDS = ["B02", "B03", "B04", "B08", "B8A", "B11", "B12", "SCL"]
SCL_BAD = [0, 1, 3, 8, 9, 10, 11]  # no-data, saturated, cloud shadow, clouds, cirrus, snow


def sentinel2(refresh=False):
    if _fresh("sentinel2_ndvi") and not refresh:
        return
    cat = _catalog()
    rng = f"{DRY_SEASON[0]}/{DRY_SEASON[1]}"
    items = list(cat.search(collections=["sentinel-2-l2a"], bbox=BBOX, datetime=rng,
                            query={"eo:cloud_cover": {"lt": 15}}).items())
    picked = _pick(items, per_tile=4)
    _log(f"Sentinel-2: {len(items)} scenes {rng}, using {len(picked)} least-cloudy across tiles")
    ds = _load(picked, S2_BANDS, resampling="nearest")
    good = ~ds.SCL.isin(SCL_BAD)
    refl = {b: (ds[b].where(good & (ds[b] > 0)).astype("float32") / 10000.0) for b in S2_BANDS if b != "SCL"}
    med = {b: _to_grid(v.median("time", skipna=True)) for b, v in refl.items()}
    eps = 1e-6
    ndvi = (med["B08"] - med["B04"]) / (med["B08"] + med["B04"] + eps)
    iron = med["B04"] / (med["B02"] + eps)            # ferric-oxide ratio (red / blue)
    ferrous = med["B11"] / (med["B8A"] + eps)          # ferrous / Fe-Mn oxide ratio (SWIR1 / NIR)
    clay = med["B11"] / (med["B12"] + eps)             # hydroxyl / clay ratio (SWIR1 / SWIR2)
    albedo = np.nanmean([med[b] for b in ("B02", "B03", "B04", "B08", "B11", "B12")], axis=0)
    # Mn oxides (pyrolusite, psilomelane) are dark and flat across VNIR–SWIR; gondite outcrops pair
    # that darkness with a ferrous signal. Index = ferrous ratio scaled by darkness.
    mn = ferrous * (1 - np.clip(albedo / 0.35, 0, 1))
    prov = dict(source="Microsoft Planetary Computer", collection="sentinel-2-l2a", licence="Copernicus open licence",
                date_range=rng, scenes=[i.id for i in picked], method="SCL cloud mask, per-pixel median",
                tiles=sorted({i.properties.get("s2:mgrs_tile") for i in picked}))
    _save_raster("sentinel2_ndvi", ndvi, prov, product="NDVI (B8−B4)/(B8+B4), dry-season median")
    _save_raster("sentinel2_mn_ratio", mn, prov, product="Fe–Mn oxide index: (B11/B8A)·(1−albedo/0.35)")
    _save_raster("sentinel2_iron_ratio", iron, prov, product="Ferric-oxide ratio B4/B2")
    _save_raster("sentinel2_clay_ratio", clay, prov, product="Clay/hydroxyl ratio B11/B12")
    _save_raster("sentinel2_rgb", np.stack([med["B04"], med["B03"], med["B02"]]), prov, product="True-colour median (R,G,B)")


# ---- Sentinel-1 -----------------------------------------------------------------------------
def sentinel1(refresh=False):
    if _fresh("sentinel1_vv") and not refresh:
        return
    cat = _catalog()
    rng = f"{DRY_SEASON[0]}/{DRY_SEASON[1]}"
    items = list(cat.search(collections=["sentinel-1-rtc"], bbox=BBOX, datetime=rng).items())
    items = items[:24]
    _log(f"Sentinel-1 RTC: using {len(items)} scenes {rng}")
    ds = _load(items, ["vv", "vh"])
    db = {b: _to_grid(10 * np.log10(ds[b].where(ds[b] > 0).median("time", skipna=True))) for b in ("vv", "vh")}
    prov = dict(source="Microsoft Planetary Computer", collection="sentinel-1-rtc", licence="Copernicus open licence",
                date_range=rng, scenes=[i.id for i in items], method="radiometric terrain corrected γ0, median, dB")
    _save_raster("sentinel1_vv", db["vv"], prov, product="VV backscatter dB")
    _save_raster("sentinel1_vh", db["vh"], prov, product="VH backscatter dB")


# ---- Copernicus DEM -------------------------------------------------------------------------
def dem(refresh=False):
    if _fresh("srtm_dem") and not refresh:
        return
    cat = _catalog()
    items = list(cat.search(collections=["cop-dem-glo-30"], bbox=BBOX).items())
    _log(f"Copernicus DEM GLO-30: {len(items)} tiles")
    ds = _load(items, ["data"])
    elev = _to_grid(ds["data"].max("time"))
    prov = dict(source="Microsoft Planetary Computer", collection="cop-dem-glo-30",
                licence="Copernicus DEM licence (free)", scenes=[i.id for i in items], method="30 m DSM averaged to 0.01°")
    # file name kept for pipeline compatibility; content is Copernicus GLO-30, not SRTM
    _save_raster("srtm_dem", elev, prov, product="Copernicus GLO-30 elevation (m)")


# ---- MODIS LST ------------------------------------------------------------------------------
def modis_lst(refresh=False):
    if _fresh("modis_lst") and not refresh:
        return
    cat = _catalog()
    rng = f"{DRY_SEASON[0]}/{DRY_SEASON[1]}"
    items = list(cat.search(collections=["modis-11A2-061"], bbox=BBOX, datetime=rng).items())
    _log(f"MODIS MOD11A2: {len(items)} granules {rng}")
    ds = _load(items, ["LST_Day_1km"], resampling="nearest")
    lst = ds["LST_Day_1km"].where(ds["LST_Day_1km"] > 0) * 0.02 - 273.15
    arr = _to_grid(lst.median("time", skipna=True))
    prov = dict(source="Microsoft Planetary Computer", collection="modis-11A2-061", licence="NASA open data",
                date_range=rng, scenes=[i.id for i in items], method="8-day day LST, scale 0.02 K, median, °C")
    _save_raster("modis_lst", arr, prov, product="MODIS day LST °C, dry-season median")


# ---- Open-Meteo (ERA5 archive + forecast) ---------------------------------------------------
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"


def _get(url, params, tries=4):
    for k in range(tries):
        try:
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 200:
                return r.json()
            _log(f"{url.split('/')[2]} → HTTP {r.status_code}, retry {k + 1}")
        except requests.RequestException as e:
            _log(f"{url.split('/')[2]} → {e.__class__.__name__}, retry {k + 1}")
        time.sleep(2 * (k + 1))
    raise RuntimeError(f"failed: {url}")


def mine_weather(refresh=False):
    """Real daily rain + soil moisture at every mine, HISTORY_START → yesterday (ERA5 / ERA5-Land)."""
    out = EXT / "imd_station_daily_rain.csv"
    end = AS_OF - timedelta(days=1)
    prov = EXT / "imd_station_daily_rain.provenance.json"
    if out.exists() and prov.exists() and not refresh and pd.read_csv(out, usecols=["date"]).date.max() >= str(end):
        return  # already up to yesterday
    j = _get(ARCHIVE, dict(latitude=",".join(str(m["lat"]) for m in MINES), longitude=",".join(str(m["lon"]) for m in MINES),
                           start_date=str(HISTORY_START), end_date=str(end),
                           daily="precipitation_sum,soil_moisture_0_to_7cm_mean", timezone="Asia/Kolkata"))
    j = j if isinstance(j, list) else [j]
    rows = []
    for m, loc in zip(MINES, j):
        d = loc["daily"]
        for t, p, sm in zip(d["time"], d["precipitation_sum"], d.get("soil_moisture_0_to_7cm_mean") or [None] * len(d["time"])):
            rows.append(dict(station=m["id"], lat=m["lat"], lon=m["lon"], date=t, rain_mm=p, soil_moisture=sm))
    df = pd.DataFrame(rows)
    EXT.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    (EXT / "imd_station_daily_rain.provenance.json").write_text(json.dumps(dict(
        name="mine_daily_weather", real=True, source="Open-Meteo historical API (ERA5 / ERA5-Land reanalysis)",
        licence="CC BY 4.0", date_range=f"{HISTORY_START}/{end}", points=[m["id"] for m in MINES],
        fetched_at=datetime.now().isoformat(timespec="seconds")), indent=2))
    _log(f"mine weather: {len(df)} rows, {df.rain_mm.isna().mean():.1%} missing")


def forecast(refresh=True):
    """Real 16-day forecast per mine; days 17–30 fall back to the ERA5 climatology for that date."""
    j = _get(FORECAST, dict(latitude=",".join(str(m["lat"]) for m in MINES), longitude=",".join(str(m["lon"]) for m in MINES),
                            daily="precipitation_sum,precipitation_probability_max", forecast_days=16, timezone="Asia/Kolkata"))
    j = j if isinstance(j, list) else [j]
    hist = pd.read_csv(EXT / "imd_station_daily_rain.csv")
    hist["doy"] = pd.to_datetime(hist.date).dt.dayofyear
    rows = []
    for m, loc in zip(MINES, j):
        d = dict(zip(loc["daily"]["time"], zip(loc["daily"]["precipitation_sum"], loc["daily"]["precipitation_probability_max"])))
        clim = hist[hist.station == m["id"]].groupby("doy").rain_mm.mean().rolling(15, center=True, min_periods=1).mean()
        for k in range(HORIZON_DAYS):
            day = AS_OF + timedelta(days=k)
            if str(day) in d:
                r, p = d[str(day)]
                rows.append(dict(station=m["id"], issued=str(AS_OF), date=str(day), rain_mm=r or 0.0,
                                 p_rain=round((p or 0) / 100, 2), source="forecast"))
            else:
                rows.append(dict(station=m["id"], issued=str(AS_OF), date=str(day),
                                 rain_mm=round(float(clim.get(day.timetuple().tm_yday, 0.0)), 1), p_rain=None, source="climatology"))
    pd.DataFrame(rows).to_csv(EXT / "imd_extended_range_forecast.csv", index=False)
    (EXT / "imd_extended_range_forecast.provenance.json").write_text(json.dumps(dict(
        name="weather_forecast", real=True, source="Open-Meteo forecast API (best-match NWP: ECMWF/GFS/ICON blend)",
        licence="CC BY 4.0", horizon="16 days forecast + ERA5 day-of-year climatology to day 30",
        issued=str(AS_OF), fetched_at=datetime.now().isoformat(timespec="seconds")), indent=2))
    _log("forecast: 16-day NWP + climatology tail written")


def climate_grid(refresh=False):
    """Annual rainfall + mean soil moisture on a 0.25° grid of ERA5 points, resampled to the analysis grid."""
    if _fresh("imd_rain_annual") and not refresh:
        return
    cb = dict(lat_min=21.00, lat_max=22.25, lon_min=78.75, lon_max=80.75)
    lats = np.arange(cb["lat_max"] - 0.125, cb["lat_min"], -0.25)
    lons = np.arange(cb["lon_min"] + 0.125, cb["lon_max"], 0.25)
    pts = [(la, lo) for la in lats for lo in lons]
    year = AS_OF.year - 1
    rain = np.zeros(len(pts)); sm = np.zeros(len(pts))
    for s in range(0, len(pts), 20):  # API batches
        chunk = pts[s:s + 20]
        j = _get(ARCHIVE, dict(latitude=",".join(f"{a:.3f}" for a, _ in chunk), longitude=",".join(f"{b:.3f}" for _, b in chunk),
                               start_date=f"{year}-01-01", end_date=f"{year}-12-31",
                               daily="precipitation_sum,soil_moisture_0_to_7cm_mean", timezone="Asia/Kolkata"))
        j = j if isinstance(j, list) else [j]
        for i, loc in enumerate(j):
            rain[s + i] = np.nansum(np.array(loc["daily"]["precipitation_sum"], dtype=float))
            sm[s + i] = np.nanmean(np.array(loc["daily"]["soil_moisture_0_to_7cm_mean"], dtype=float))
    rain_g = geo.resample_coarse(rain.reshape(len(lats), len(lons)), cb, 0.25)
    sm_g = geo.resample_coarse(sm.reshape(len(lats), len(lons)), cb, 0.25)
    prov = dict(source="Open-Meteo historical API (ERA5 / ERA5-Land)", licence="CC BY 4.0", year=year,
                method=f"{len(pts)} reanalysis points at 0.25°, bilinear to 0.01°")
    _save_raster("imd_rain_annual", rain_g, prov, product=f"Annual rainfall {year} (mm, ERA5)")
    _save_raster("smap_soil_moisture", sm_g, prov, product=f"Mean soil moisture 0–7 cm {year} (m³/m³, ERA5-Land)")


STEPS = [("Sentinel-2", sentinel2), ("Sentinel-1", sentinel1), ("Copernicus DEM", dem), ("MODIS LST", modis_lst),
         ("ERA5 climate grid", climate_grid), ("mine weather", mine_weather)]


def fetch_all(refresh=False, log=_log):
    status = {}
    for name, fn in STEPS:
        t = time.time()
        try:
            fn(refresh=refresh)
            status[name] = dict(ok=True, secs=round(time.time() - t, 1))
        except Exception as e:  # keep going; the pipeline falls back to simulated layers for this one
            status[name] = dict(ok=False, error=repr(e)[:300])
            log(f"{name} FAILED: {e!r}")
    try:
        forecast()
        status["forecast"] = dict(ok=True)
    except Exception as e:
        status["forecast"] = dict(ok=False, error=repr(e)[:300])
        log(f"forecast FAILED: {e!r}")
    return status


if __name__ == "__main__":
    print(json.dumps(fetch_all(refresh="--refresh" in sys.argv), indent=2))


# ---- fine-resolution stack for the U-Net (Phase 4) -------------------------------------------
UNET_RES = RES / 5  # 0.002° ≈ 220 m → 400 × 750 px; 5×5 blocks pool exactly onto the analysis grid
UNET_BANDS = ["B02", "B03", "B04", "B08", "B8A", "B11", "B12"]


def unet_stack(refresh=False):
    """Real Sentinel-2 dry-season median (7 bands + NDVI) and Copernicus DEM (+ slope) at 0.002°."""
    if (SAT / "unet_stack.npz").exists() and (SAT / "unet_stack.provenance.json").exists() and not refresh:
        return
    cat = _catalog()
    rng_ = f"{DRY_SEASON[0]}/{DRY_SEASON[1]}"
    items = _pick(list(cat.search(collections=["sentinel-2-l2a"], bbox=BBOX, datetime=rng_,
                                  query={"eo:cloud_cover": {"lt": 15}}).items()), per_tile=4)
    _log(f"U-Net stack: {len(items)} Sentinel-2 scenes at {UNET_RES:.4f} deg")
    ds = _load(items, UNET_BANDS + ["SCL"], resolution=UNET_RES, resampling="nearest")
    good = ~ds.SCL.isin(SCL_BAD)
    H, W = round((BOUNDS["lat_max"] - BOUNDS["lat_min"]) / UNET_RES), round((BOUNDS["lon_max"] - BOUNDS["lon_min"]) / UNET_RES)
    lat = BOUNDS["lat_max"] - (np.arange(H) + 0.5) * UNET_RES
    lon = BOUNDS["lon_min"] + (np.arange(W) + 0.5) * UNET_RES
    sel = lambda da: np.asarray(da.sel(latitude=lat, longitude=lon, method="nearest").values, dtype="float32")
    bands = [sel(ds[b].where(good & (ds[b] > 0)).median("time", skipna=True) / 10000.0) for b in UNET_BANDS]
    ndvi = (bands[3] - bands[2]) / (bands[3] + bands[2] + 1e-6)
    dem_items = list(cat.search(collections=["cop-dem-glo-30"], bbox=BBOX).items())
    z = sel(_load(dem_items, ["data"], resolution=UNET_RES)["data"].max("time"))
    gy, gx = np.gradient(z, UNET_RES * 110_570, UNET_RES * 103_400)
    slope = np.degrees(np.arctan(np.hypot(gx, gy))).astype("float32")
    stack = np.stack(bands + [ndvi, z, slope])
    names = UNET_BANDS + ["NDVI", "elevation", "slope"]
    np.savez_compressed(SAT / "unet_stack.npz", data=stack, bounds=json.dumps(BOUNDS), res=UNET_RES,
                        meta=json.dumps(dict(bands=names)))
    (SAT / "unet_stack.provenance.json").write_text(json.dumps(dict(
        name="unet_stack", real=True, source="Microsoft Planetary Computer", collection="sentinel-2-l2a + cop-dem-glo-30",
        licence="Copernicus open licence", date_range=rng_, scenes=[i.id for i in items] + [i.id for i in dem_items],
        method=f"SCL-masked median at {UNET_RES:.4f} deg; bands {', '.join(names)}",
        fetched_at=datetime.now().isoformat(timespec="seconds")), indent=2))
    _log(f"U-Net stack {stack.shape}, NaN {np.isnan(stack).mean():.2%}")
