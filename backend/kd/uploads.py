"""Layer 2 · Manual uploads (the diagram's "Manual Uploads (Drilling, Maps)").

Authorised users upload real survey data; each file is validated, stored in the data lake under
`uploads/`, versioned in the `uploads` table, and the latest accepted upload of each kind takes
precedence over open-data or simulated equivalents on the next pipeline run:

  boreholes   CSV/XLSX  hole_id, lat, lon, thickness_m, grade_mn_pct [, depth_to_ore_m, hole_depth_m, drilled_on]
  geochem     CSV/XLSX  sample_id, lat, lon, mn_ppm
  aeromag     GeoTIFF   any single-band grid in EPSG:4326 (e.g. GSI NGPM aeromagnetic anomaly, nT)
  geology     ZIP(shp) or GeoJSON  polygons in EPSG:4326 with a unit-name attribute (e.g. GSI Bhukosh lithology)
"""
import io
import json
import zipfile
from datetime import datetime

import numpy as np
import pandas as pd

from . import geo, storage
from .config import BOUNDS, LAKE, REGISTRY, RES

UP = LAKE / "uploads"
KINDS = {
    "boreholes": dict(label="Borehole logs", formats=".csv, .xlsx",
                      required=["hole_id", "lat", "lon", "thickness_m", "grade_mn_pct"],
                      optional=["depth_to_ore_m", "hole_depth_m", "drilled_on"]),
    "geochem": dict(label="Stream-sediment / soil geochemistry", formats=".csv, .xlsx",
                    required=["sample_id", "lat", "lon", "mn_ppm"], optional=[]),
    "operations": dict(label="Mine operations (MIS export)", formats=".csv, .xlsx",
                       required=["type", "mine_id"], optional=["date", "shift", "tonnes", "dumpers_total", "dumpers_available",
                                                               "excavator_availability_pct", "downtime_hrs", "blasts", "explosive_kg",
                                                               "rostered", "present", "month", "target_t"]),
    "aeromag": dict(label="Aeromagnetic grid", formats=".tif, .tiff (EPSG:4326)", required=[], optional=[]),
    "geology": dict(label="Geology / lithology map", formats=".zip (shapefile) or .geojson (EPSG:4326)", required=[], optional=[]),
}
UNIT_FIELDS = ("lithology", "litho", "unit_name", "unit", "name", "group_name", "formation", "lith", "descriptio", "description")


class UploadError(ValueError):
    pass


def _table(raw, name):
    if name.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(raw))
    else:
        df = pd.read_csv(io.BytesIO(raw))
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    return df


def _check_points(df, kind):
    spec = KINDS[kind]
    missing = [c for c in spec["required"] if c not in df.columns]
    if missing:
        raise UploadError(f"missing column(s): {', '.join(missing)}. Required: {', '.join(spec['required'])}")
    df = df[spec["required"] + [c for c in spec["optional"] if c in df.columns]].copy()
    for c in ("lat", "lon") + tuple(c for c in spec["required"] if c not in ("hole_id", "sample_id", "lat", "lon")):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    bad = df[["lat", "lon"]].isna().any(axis=1)
    inside = df.lat.between(BOUNDS["lat_min"], BOUNDS["lat_max"]) & df.lon.between(BOUNDS["lon_min"], BOUNDS["lon_max"])
    notes = []
    if bad.any():
        notes.append(f"{int(bad.sum())} row(s) without valid coordinates dropped")
    if (~inside & ~bad).any():
        notes.append(f"{int((~inside & ~bad).sum())} row(s) outside the study area dropped")
    df = df[~bad & inside]
    if kind == "boreholes":
        if ((df.grade_mn_pct < 0) | (df.grade_mn_pct > 70)).any():
            raise UploadError("grade_mn_pct must be a percentage between 0 and 70")
        if (df.thickness_m < 0).any():
            raise UploadError("thickness_m must be ≥ 0")
    if kind == "geochem" and (df.mn_ppm <= 0).any():
        raise UploadError("mn_ppm must be > 0")
    if len(df) < 5:
        raise UploadError(f"only {len(df)} usable row(s) inside the study area; need at least 5")
    return df, notes


def _aeromag(raw):
    import rasterio
    from rasterio.warp import Resampling, reproject
    from rasterio.transform import from_origin
    with rasterio.MemoryFile(raw) as mf, mf.open() as src:
        if src.crs is None:
            raise UploadError("GeoTIFF has no CRS; export it with a CRS (EPSG:4326 preferred)")
        dst = np.full(geo.grid()[0].shape, np.nan, dtype="float32")  # any CRS is reprojected to the grid
        reproject(source=rasterio.band(src, 1), destination=dst, src_transform=src.transform, src_crs=src.crs,
                  src_nodata=src.nodata, dst_transform=from_origin(BOUNDS["lon_min"], BOUNDS["lat_max"], RES, RES),
                  dst_crs="EPSG:4326", dst_nodata=np.nan, resampling=Resampling.bilinear)
    cover = float(np.isfinite(dst).mean())
    if cover < 0.2:
        raise UploadError(f"grid covers only {cover:.0%} of the study area")
    return dst, [f"covers {cover:.0%} of the study area"]


def _geology(raw, name):
    from rasterio.features import rasterize
    from rasterio.transform import from_origin
    if name.lower().endswith(".zip"):
        import shapefile
        z = zipfile.ZipFile(io.BytesIO(raw))
        shp = next((n for n in z.namelist() if n.lower().endswith(".shp")), None)
        if not shp:
            raise UploadError("zip has no .shp file")
        base = shp[:-4]
        prj = next((n for n in z.namelist() if n.lower() == (base + ".prj").lower()), None)
        if prj and "GEOGCS" not in z.read(prj).decode(errors="ignore").upper()[:12] and "PROJCS" in z.read(prj).decode(errors="ignore").upper():
            raise UploadError("shapefile is in a projected CRS; re-export it in EPSG:4326 (WGS84 lat/lon)")
        rd = shapefile.Reader(shp=io.BytesIO(z.read(shp)), dbf=io.BytesIO(z.read(base + ".dbf")))
        fields = [f[0].lower() for f in rd.fields[1:]]
        feats = [(sr.shape.__geo_interface__, dict(zip(fields, sr.record))) for sr in rd.shapeRecords()]
    else:
        fc = json.loads(raw)
        feats = [(f["geometry"], {k.lower(): v for k, v in (f.get("properties") or {}).items()}) for f in fc.get("features", [])]
    if not feats:
        raise UploadError("no polygons found")
    field = next((f for f in UNIT_FIELDS if f in feats[0][1]), None)
    if not field:
        raise UploadError(f"no unit-name attribute; expected one of: {', '.join(UNIT_FIELDS)}")
    names = sorted({str(p.get(field) or "unknown").strip() for _, p in feats})
    codes = {n: i for i, n in enumerate(names)}
    shape = geo.grid()[0].shape
    arr = rasterize(((g, codes[str(p.get(field) or "unknown").strip()]) for g, p in feats), out_shape=shape,
                    transform=from_origin(BOUNDS["lon_min"], BOUNDS["lat_max"], RES, RES), fill=-1, dtype="int32").astype(float)
    arr[arr < 0] = np.nan
    cover = float(np.isfinite(arr).mean())
    if cover < 0.2:
        raise UploadError(f"polygons cover only {cover:.0%} of the study area; check the CRS is EPSG:4326")
    return arr, {i: n for n, i in codes.items()}, [f"{len(feats)} polygons, {len(names)} units from '{field}', covers {cover:.0%}"]


def ingest_upload(kind, filename, raw, user):
    """Validate + store one upload. Returns the upload record (status accepted/rejected)."""
    if kind not in KINDS:
        raise UploadError(f"unknown upload kind '{kind}'")
    UP.mkdir(parents=True, exist_ok=True)
    ts = datetime.now()
    uid = ts.strftime("%Y%m%d%H%M%S") + f"_{kind}"
    rec = dict(upload_id=uid, ts=ts.isoformat(timespec="seconds"), kind=kind, filename=filename, user=user,
               status="rejected", rows=0, message="", active=0)
    try:
        if kind == "operations":
            from . import ingest
            df = _table(raw, filename)
            if not {"type", "mine_id"} <= set(df.columns):
                raise UploadError("missing column(s): type, mine_id. Each row is one record: production, equipment, blasting, manpower or plan")
            recs = [{k: v for k, v in r.items() if pd.notna(v)} for r in df.to_dict("records")]
            res = ingest.accept(recs, channel="upload", source=f"upload:{filename}")
            if res["accepted"] == 0:
                raise UploadError(f"no valid records ({res['rejected']} rejected): {'; '.join(res['errors'][:3])}")
            rec.update(rows=res["accepted"], message=f"{res['accepted']} records ingested, {res['rejected']} dead-lettered"
                       + (f" (e.g. {res['errors'][0]})" if res["errors"] else ""))
        elif kind in ("boreholes", "geochem"):
            df, notes = _check_points(_table(raw, filename), kind)
            df.to_csv(UP / f"{uid}.csv", index=False)
            rec.update(rows=len(df), message="; ".join(notes) or "ok")
        elif kind == "aeromag":
            arr, notes = _aeromag(raw)
            np.savez_compressed(UP / f"{uid}.npz", data=arr, bounds=json.dumps(BOUNDS), res=RES, meta=json.dumps({}))
            rec.update(rows=int(np.isfinite(arr).sum()), message="; ".join(notes))
        else:
            arr, legend, notes = _geology(raw, filename)
            np.savez_compressed(UP / f"{uid}.npz", data=arr, bounds=json.dumps(BOUNDS), res=RES,
                                meta=json.dumps(dict(legend=legend)))
            rec.update(rows=len(legend), message="; ".join(notes))
        (UP / f"{uid}.source{_ext(filename)}").write_bytes(raw)  # keep the original in the lake
        rec.update(status="accepted", active=1)
        if _has_table():
            storage.execute("UPDATE uploads SET active=0 WHERE kind=?", (kind,), db=REGISTRY)
    except UploadError as e:
        rec["message"] = str(e)
    except Exception as e:  # unreadable file etc.
        rec["message"] = f"could not read file: {e.__class__.__name__}: {str(e)[:200]}"
    storage.append_rows("uploads", [rec], db=REGISTRY)
    storage.log_run(2, "manual upload", kind=kind, status=rec["status"], rows=rec["rows"])
    return rec


def _ext(name):
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _has_table():
    try:
        storage.read_table("uploads", db=REGISTRY, where="LIMIT 1")
        return True
    except Exception:
        return False


def history():
    return storage.read_table("uploads", db=REGISTRY).sort_values("ts", ascending=False).to_dict("records") if _has_table() else []


def active(kind):
    """Path stem of the active accepted upload of this kind, or None."""
    if not _has_table():
        return None
    df = storage.read_table("uploads", db=REGISTRY)
    df = df[(df.kind == kind) & (df.status == "accepted") & (df.active == 1)]
    return None if df.empty else df.sort_values("ts").iloc[-1].to_dict()


def deactivate(upload_id):
    storage.execute("UPDATE uploads SET active=0 WHERE upload_id=?", (upload_id,), db=REGISTRY)


def template(kind):
    spec = KINDS[kind]
    cols = spec["required"] + spec["optional"]
    if kind == "operations":
        return "".join(r + "\n" for r in (
            "type,mine_id,date,shift,tonnes,dumpers_total,dumpers_available,excavator_availability_pct,downtime_hrs,blasts,explosive_kg,rostered,present,month,target_t",
            "production,BLG,2026-10-10,A,512.4,,,,,,,,,,",
            "equipment,BLG,2026-10-10,,,14,12,88.5,8.3,,,,,,",
            "blasting,BLG,2026-10-10,,,,,,,3,3400,,,,",
            "manpower,BLG,2026-10-10,,,,,,,,,311,289,,",
            "plan,BLG,,,,,,,,,,,,2026-11,46500",
        ))
    examples = {
        "boreholes": ["BH-0001", 21.85, 80.23, 6.4, 34.2, 42.0, 180.0, "2019-02-11"],
        "geochem": ["SS-0001", 21.62, 79.88, 1840.0],
    }
    return ",".join(cols) + "\n" + ",".join(str(v) for v in examples[kind]) + "\n"


def apply_uploads(raw):
    """Swap the latest accepted upload of each kind into the ingested data (before processing)."""
    applied = {}

    def prov(rec):
        return dict(real=True, source=f"Upload · {rec['filename']} (by {rec['user']})", date_range=rec["ts"][:10],
                    method=rec["message"], licence="as supplied by the uploader", fetched_at=rec["ts"])

    rec = active("boreholes")
    if rec:
        raw["tables"]["boreholes"] = pd.read_csv(UP / f"{rec['upload_id']}.csv")
        raw["provenance"]["boreholes"] = prov(rec)
        applied["boreholes"] = rec["upload_id"]
    rec = active("geochem")
    if rec:
        raw["tables"]["geochem_stream_sediment"] = pd.read_csv(UP / f"{rec['upload_id']}.csv")
        raw["provenance"]["geochem_stream_sediment"] = prov(rec)
        applied["geochem"] = rec["upload_id"]
    rec = active("aeromag")
    if rec:
        z = np.load(UP / f"{rec['upload_id']}.npz")
        raw["rasters"]["emag2_mag"] = (z["data"].astype(float), BOUNDS, RES, {})
        raw["provenance"]["emag2_mag"] = prov(rec)
        applied["aeromag"] = rec["upload_id"]
    rec = active("geology")
    if rec:
        z = np.load(UP / f"{rec['upload_id']}.npz")
        raw["rasters"]["macrostrat_units"] = (z["data"].astype(float), BOUNDS, RES, json.loads(str(z["meta"])))
        raw["provenance"]["macrostrat_units"] = prov(rec)
        applied["geology"] = rec["upload_id"]
    return applied
