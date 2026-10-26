"""Layer 1 · REAL geology, geophysics and mineral-occurrence sources (Phase 2).

  USGS MRDS        mrdata.usgs.gov WFS          recorded manganese sites (producers, past producers, occurrences)
  EMAG2 v3         NOAA NCEI ImageServer        magnetic anomaly grid (2 arc-min), clipped to the belt
  Macrostrat       macrostrat.org API           lithology / age units (global 'tiny' scale), 0.05° lattice
  Lineaments       derived from Copernicus DEM  multi-azimuth hillshade edges → distance to lineament
  Rivers           OpenStreetMap (Overpass)     waterway=river

Each output lands in the data lake with a provenance sidecar ("real": true). GSI Bhukosh files,
when uploaded, take precedence over these (see kd/uploads.py).
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np
import pandas as pd
import requests
from scipy import ndimage

from . import geo
from .config import BOUNDS, LAKE, RES

GEOL = LAKE / "geology"
UA = {"User-Agent": "KhanijSetu/1.0 (SIH research prototype)"}
PAD = 0.3  # fetch a margin around the study area so edges are well-defined


def _log(msg):
    print(f"[geology] {msg}", flush=True)


def _prov(name, **kw):
    GEOL.mkdir(parents=True, exist_ok=True)
    (GEOL / f"{name}.provenance.json").write_text(json.dumps(
        dict(name=name, real=True, fetched_at=datetime.now().isoformat(timespec="seconds"), **kw), indent=2))


def _fresh(name, ext):
    return (GEOL / f"{name}.{ext}").exists() and (GEOL / f"{name}.provenance.json").exists()


def _save_raster(name, arr, meta, **prov):
    GEOL.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(GEOL / f"{name}.npz", data=arr.astype(np.float32), bounds=json.dumps(BOUNDS), res=RES,
                        meta=json.dumps(meta))
    _prov(name, **prov)


# ---- USGS MRDS ------------------------------------------------------------------------------
REGION_RECORD = re.compile(r"\b(?:area|states?)\b", re.I)  # province-level records with no real site position


def mrds(refresh=False):
    if _fresh("usgs_mrds_mn", "csv") and not refresh:
        return
    b = BOUNDS
    x = requests.get("https://mrdata.usgs.gov/services/wfs/mrds", headers=UA, timeout=120, params=dict(
        request="GetFeature", service="WFS", version="1.1.0", typeName="mrds", maxFeatures=2000,
        bbox=f"{b['lat_min'] - PAD},{b['lon_min'] - PAD},{b['lat_max'] + PAD},{b['lon_max'] + PAD},EPSG:4326")).text
    rows = []
    for fid, body in re.findall(r'<ms:mrds gml:id="([^"]+)">(.*?)</ms:mrds>', x, re.S):
        g = lambda t: (re.search(rf"<ms:{t}>(.*?)</ms:{t}>", body, re.S) or [None, ""])[1].strip()
        lat, lon = map(float, re.search(r"<gml:pos>([^<]+)</gml:pos>", body).group(1).split())
        rows.append(dict(dep_id=g("dep_id"), site=g("site_name"), status=g("dev_stat"), commodities=g("code_list"),
                         lat=lat, lon=lon, url=g("url")))
    df = pd.DataFrame(rows)
    df = df[df.commodities.str.contains(r"\bMN\b")]
    df["region_record"] = df.site.str.contains(REGION_RECORD)
    df = df.drop_duplicates(["site", "lat", "lon"])
    df.to_csv(GEOL / "usgs_mrds_mn.csv", index=False)
    _prov("usgs_mrds_mn", source="USGS Mineral Resources Data System (MRDS) WFS", licence="US public domain",
          method="commodity code MN; province-level records flagged region_record and not used as point labels",
          records=len(df), by_status=df.status.value_counts().to_dict())
    _log(f"MRDS: {len(df)} manganese records ({int(df.region_record.sum())} region-level)")


# ---- EMAG2 v3 -------------------------------------------------------------------------------
def emag2(refresh=False):
    if _fresh("emag2_mag", "npz") and not refresh:
        return
    import rasterio
    cb = dict(lat_min=BOUNDS["lat_min"] - PAD, lat_max=BOUNDS["lat_max"] + PAD,
              lon_min=BOUNDS["lon_min"] - PAD, lon_max=BOUNDS["lon_max"] + PAD)
    res = 1 / 60  # EMAG2v3 native is 2'; request at 1' so bilinear to 0.01° is smooth
    w, h = round((cb["lon_max"] - cb["lon_min"]) / res), round((cb["lat_max"] - cb["lat_min"]) / res)
    r = requests.get("https://gis.ngdc.noaa.gov/arcgis/rest/services/EMAG2v3/ImageServer/exportImage", headers=UA, timeout=180,
                     params=dict(bbox=f"{cb['lon_min']},{cb['lat_min']},{cb['lon_max']},{cb['lat_max']}", bboxSR=4326, imageSR=4326,
                                 size=f"{w},{h}", format="tiff", pixelType="F32", interpolation="RSP_BilinearInterpolation", f="image"))
    r.raise_for_status()
    with rasterio.MemoryFile(r.content) as mf, mf.open() as ds:
        a = ds.read(1).astype(float)
    a[a < -1e20] = np.nan
    grid = geo.resample_coarse(a, cb, res)
    _save_raster("emag2_mag", grid, dict(product="EMAG2v3 magnetic anomaly (nT), 4 km upward continued"),
                 source="NOAA NCEI EMAG2 v3 ImageServer", licence="US public domain",
                 method="exportImage subset at 1′, bilinear to 0.01°", collection="EMAG2v3")
    _log(f"EMAG2: {np.nanmin(grid):.0f}…{np.nanmax(grid):.0f} nT")


# ---- Macrostrat lithology -------------------------------------------------------------------
def _macro(pt):
    for _ in range(3):
        try:
            d = requests.get("https://macrostrat.org/api/v2/geologic_units/map", headers=UA, timeout=40,
                             params=dict(lat=pt[0], lng=pt[1], scale="tiny")).json()["success"]["data"]
            return d[0] if d else None
        except Exception:
            continue
    return None


def macrostrat(refresh=False, step=0.05):
    if _fresh("macrostrat_units", "npz") and not refresh:
        return
    lats = np.arange(BOUNDS["lat_max"] - step / 2, BOUNDS["lat_min"], -step)
    lons = np.arange(BOUNDS["lon_min"] + step / 2, BOUNDS["lon_max"], step)
    pts = [(round(la, 3), round(lo, 3)) for la in lats for lo in lons]
    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(_macro, pts))
    names = sorted({r["name"] for r in res if r})
    codes = {n: i for i, n in enumerate(names)}
    lattice = np.array([codes[r["name"]] if r else np.nan for r in res], dtype=float).reshape(len(lats), len(lons))
    # nearest-neighbour onto the 0.01° grid (categorical: never interpolate classes)
    lat2d, lon2d = geo.grid()
    ri = np.clip(np.round((lats[0] - lat2d) / step).astype(int), 0, len(lats) - 1)
    ci = np.clip(np.round((lon2d - lons[0]) / step).astype(int), 0, len(lons) - 1)
    units = lattice[ri, ci]
    legend = {i: n for n, i in codes.items()}
    detail = {n: next((dict(lith=r["lith"], age=r.get("t_int_name"), source_id=r.get("source_id")) for r in res if r and r["name"] == n), {}) for n in names}
    _save_raster("macrostrat_units", units, dict(product="Lithology / age units (Macrostrat tiny scale)", legend=legend, detail=detail),
                 source="Macrostrat API (geologic_units/map, scale=tiny)", licence="CC BY 4.0",
                 method=f"{len(pts)}-point lattice at {step}°, nearest-neighbour to 0.01°", legend=legend)
    _log(f"Macrostrat: {len(names)} units over {len(pts)} points")


# ---- DEM lineaments -------------------------------------------------------------------------
def dem_lineaments(refresh=False, fine=0.0025):
    """Lineaments from the real Copernicus DEM: hillshade from 4 sun azimuths, Sobel edge strength,
    keep the strongest linear edges, then distance (km) from every 0.01° cell to the nearest one."""
    if _fresh("dem_lineament_km", "npz") and not refresh:
        return
    from .eo import BBOX, _catalog, _load
    cat = _catalog()
    items = list(cat.search(collections=["cop-dem-glo-30"], bbox=BBOX).items())
    da = _load(items, ["data"], resolution=fine)["data"].max("time")
    z = np.asarray(da.values, dtype=float)
    lat = np.asarray(da.latitude.values)
    px_m = fine * 110_570
    gy, gx = np.gradient(z, px_m, px_m * np.cos(np.radians(lat.mean())))
    edges = np.zeros_like(z)
    for az in (0, 45, 90, 135):
        a = np.radians(az)
        shade = np.cos(np.radians(45)) * np.cos(np.arctan(np.hypot(gx, gy))) + \
            np.sin(np.radians(45)) * np.sin(np.arctan(np.hypot(gx, gy))) * np.cos(a - np.arctan2(-gx, gy))
        edges = np.maximum(edges, np.hypot(ndimage.sobel(shade, 0), ndimage.sobel(shade, 1)))
    edges = ndimage.gaussian_filter(edges, 1)
    mask = edges > np.nanpercentile(edges, 96)
    mask = ndimage.binary_opening(mask, structure=np.ones((1, 3))) | ndimage.binary_opening(mask, structure=np.ones((3, 1)))
    mask = ndimage.remove_small_objects(mask, 12) if hasattr(ndimage, "remove_small_objects") else _drop_small(mask, 12)
    dist_px = ndimage.distance_transform_edt(~mask)
    dist_km = dist_px * fine * 110.57
    from .eo import _to_grid
    da_d = da.copy(data=dist_km)
    grid = _to_grid(da_d)
    _save_raster("dem_lineament_km", grid, dict(product="Distance to DEM-derived lineament (km)"),
                 source="Derived from Copernicus DEM GLO-30 (Planetary Computer)", collection="cop-dem-glo-30",
                 licence="Copernicus DEM licence (free)",
                 method=f"{fine}° DEM, 4-azimuth hillshade, Sobel edges, top 4% linear segments, Euclidean distance")
    _log(f"lineaments: {mask.mean():.1%} of fine pixels on a lineament")


def _drop_small(mask, n):
    lab, k = ndimage.label(mask)
    sizes = ndimage.sum(mask, lab, range(1, k + 1))
    keep = np.isin(lab, np.where(sizes >= n)[0] + 1)
    return keep


# ---- OSM rivers -----------------------------------------------------------------------------
OVERPASS = ["https://maps.mail.ru/osm/tools/overpass/api/interpreter", "https://overpass.private.coffee/api/interpreter",
            "https://overpass-api.de/api/interpreter"]  # (overpass.osm.ch holds Swiss data only)


def rivers(refresh=False):
    if _fresh("osm_rivers", "geojson") and not refresh:
        return
    b = BOUNDS
    q = f'[out:json][timeout:90];way["waterway"="river"]({b["lat_min"]},{b["lon_min"]},{b["lat_max"]},{b["lon_max"]});out geom;'
    for url in OVERPASS:
        try:
            r = requests.post(url, data={"data": q}, headers=UA, timeout=150)
            if r.status_code == 200 and r.json().get("elements"):
                break  # an empty answer means a regional/partial mirror: try the next one
        except (requests.RequestException, ValueError):
            continue
    else:
        raise RuntimeError("Overpass unavailable")
    feats = []
    for e in r.json().get("elements", []):
        coords = [[round(p["lon"], 5), round(p["lat"], 5)] for p in e.get("geometry", [])]
        if len(coords) > 1:
            feats.append({"type": "Feature", "properties": {"kind": "river", "id": e.get("tags", {}).get("name", "river"), "osm_id": e["id"]},
                          "geometry": {"type": "LineString", "coordinates": coords}})
    (GEOL / "osm_rivers.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    _prov("osm_rivers", source="OpenStreetMap via Overpass API", licence="ODbL 1.0", method='way["waterway"="river"]', records=len(feats))
    _log(f"rivers: {len(feats)} OSM river ways")


STEPS = [("USGS MRDS", mrds), ("EMAG2 magnetics", emag2), ("Macrostrat lithology", macrostrat),
         ("DEM lineaments", dem_lineaments), ("OSM rivers", rivers)]


def fetch_all(refresh=False, log=_log):
    status = {}
    for name, fn in STEPS:
        try:
            fn(refresh=refresh)
            status[name] = dict(ok=True)
        except Exception as e:
            status[name] = dict(ok=False, error=repr(e)[:300])
            log(f"{name} FAILED: {e!r}")
    return status


if __name__ == "__main__":
    import sys
    print(json.dumps(fetch_all(refresh="--refresh" in sys.argv), indent=2))
