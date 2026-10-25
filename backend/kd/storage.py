"""Layer 3 · Data Storage.

  Data Lake            data/lake/raw/**        files exactly as ingested (written by Layer 1)
  Processed Warehouse  store 'warehouse'       cleaned, integrated tables + pipeline/alert logs
  Geospatial Database  store 'geo'             analysis grid (cells + raster layers) and vector layers;
                                               on PostgreSQL: PostGIS geometry columns with GiST indexes
  Model Feature Store  data/feature_store/     versioned training/inference feature tables

Stores are SQLite files by default and PostgreSQL schemas when KD_DATABASE_URL is set (kd/db.py).
"""
import hashlib
import json
from datetime import datetime

import numpy as np
import pandas as pd

from . import db as _db, geo
from .config import DATA, FEATURE_STORE, GEODB, LAKE, RES, WAREHOUSE


# ---- warehouse -------------------------------------------------------------------------------
def write_table(name, df, db=WAREHOUSE):
    store = _db.store_of(db)
    _db.to_sql(df, name, store, if_exists="replace")
    if store == "geo" and name == "grid_cells":
        _spatialize_grid()


def read_table(name, db=WAREHOUSE, where=""):
    df = _db.read_sql(f"SELECT * FROM {name} {where}", _db.store_of(db))
    return df.drop(columns=["geom"], errors="ignore")


def append_rows(name, rows, db=WAREHOUSE):
    if rows:
        _db.to_sql(pd.DataFrame(rows), name, _db.store_of(db), if_exists="append")


def execute(sql, params=(), db=WAREHOUSE):
    with _db.connect(db) as c:
        c.execute(sql, params)


def log_run(layer, stage, **info):
    append_rows("pipeline_runs", [dict(ts=datetime.now().isoformat(timespec="seconds"), layer=layer,
                                       stage=stage, info=json.dumps(info, default=str))])


# ---- geospatial db ---------------------------------------------------------------------------
def write_grid(layers, catalog):
    """layers: {name: 2-D array on the analysis grid}; catalog: {name: dict(unit, group, desc)}."""
    lat2d, lon2d = geo.grid()
    rows, cols = lat2d.shape
    rr, cc = np.indices((rows, cols))
    h = RES / 2
    df = pd.DataFrame(dict(cell_id=(rr * cols + cc).ravel(), row=rr.ravel(), col=cc.ravel(),
                           lat=lat2d.ravel().round(4), lon=lon2d.ravel().round(4)))
    df["wkt"] = [f"POLYGON(({lo-h:.3f} {la-h:.3f},{lo+h:.3f} {la-h:.3f},{lo+h:.3f} {la+h:.3f},"
                 f"{lo-h:.3f} {la+h:.3f},{lo-h:.3f} {la-h:.3f}))" for la, lo in zip(df.lat, df.lon)]
    for k, v in layers.items():
        df[k] = np.asarray(v, dtype=float).ravel()
    write_table("grid_cells", df, GEODB)
    write_table("layer_catalog", pd.DataFrame([dict(name=k, **v) for k, v in catalog.items()]), GEODB)


def add_grid_columns(layers, catalog):
    df = read_table("grid_cells", GEODB)
    for k, v in layers.items():
        df[k] = np.asarray(v, dtype=float).ravel()
    write_table("grid_cells", df, GEODB)
    cat = read_table("layer_catalog", GEODB)
    cat = cat[~cat.name.isin(list(catalog))]
    cat = pd.concat([cat, pd.DataFrame([dict(name=k, **v) for k, v in catalog.items()])])
    write_table("layer_catalog", cat, GEODB)


def _spatialize_grid():
    """PostGIS: a real polygon geometry per cell + GiST index (the WKT column stays for portability)."""
    if not _db.has_postgis():
        return
    with _db.connect("geo") as c:
        c.execute("ALTER TABLE grid_cells ADD COLUMN IF NOT EXISTS geom geometry(Polygon, 4326)")
        c.execute("UPDATE grid_cells SET geom = ST_GeomFromText(wkt, 4326)")
        c.execute("CREATE INDEX IF NOT EXISTS grid_cells_geom ON grid_cells USING GIST (geom)")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS grid_cells_id ON grid_cells (cell_id)")


def write_vector(name, feature_collection):
    with _db.connect(GEODB) as c:
        c.execute("CREATE TABLE IF NOT EXISTS vectors (name TEXT PRIMARY KEY, geojson TEXT)")
        c.execute("INSERT OR REPLACE INTO vectors VALUES (?, ?)", (name, json.dumps(feature_collection)))
    if _db.has_postgis():  # one row per feature with real geometry, queryable with spatial SQL
        with _db.connect("geo") as c:
            c.execute("""CREATE TABLE IF NOT EXISTS vector_features (layer TEXT, fid INTEGER, props JSONB,
                         geom geometry(Geometry, 4326), PRIMARY KEY (layer, fid))""")
            c.execute("CREATE INDEX IF NOT EXISTS vector_features_geom ON vector_features USING GIST (geom)")
            c.execute("DELETE FROM vector_features WHERE layer=?", (name,))
            c.executemany("INSERT INTO vector_features VALUES (?, ?, ?::jsonb, ST_SetSRID(ST_GeomFromGeoJSON(?), 4326))",
                          [(name, i, json.dumps(f.get("properties") or {}, default=str), json.dumps(f["geometry"]))
                           for i, f in enumerate(feature_collection.get("features", [])) if f.get("geometry")])


def read_vector(name):
    if _db.has_postgis():
        with _db.connect("geo") as c:
            if c.execute("SELECT to_regclass('geo.vector_features') IS NOT NULL").fetchone()[0]:
                rows = c.execute("SELECT props, ST_AsGeoJSON(geom, 6) AS g FROM vector_features WHERE layer=? ORDER BY fid",
                                 (name,)).fetchall()
                if rows:
                    return {"type": "FeatureCollection", "features": [
                        {"type": "Feature", "geometry": json.loads(r["g"]), "properties": r["props"]} for r in rows]}
    with _db.connect(GEODB) as c:
        row = c.execute("SELECT geojson FROM vectors WHERE name=?", (name,)).fetchone()
    return json.loads(row[0]) if row else {"type": "FeatureCollection", "features": []}


def spatial_lookup(lat, lon):
    """Which grid cell and reserve zone contain a point: PostGIS ST_Contains when available."""
    if _db.has_postgis():
        with _db.connect("geo") as c:
            cell = c.execute("SELECT cell_id FROM grid_cells WHERE ST_Contains(geom, ST_SetSRID(ST_Point(?, ?), 4326)) LIMIT 1",
                             (lon, lat)).fetchone()
            zone = c.execute("SELECT props->>'zone_id' AS z FROM vector_features WHERE layer='reserve_zones' "
                             "AND ST_Contains(geom, ST_SetSRID(ST_Point(?, ?), 4326)) LIMIT 1", (lon, lat)).fetchone()
        return dict(cell_id=cell[0] if cell else None, zone_id=zone["z"] if zone else None, engine="postgis")
    rc = geo.cell_of(lat, lon)
    shape = geo.grid()[0].shape
    zone = None
    for f in read_vector("reserve_zones")["features"]:
        if _point_in_ring(lon, lat, f["geometry"]["coordinates"][0]):
            zone = f["properties"].get("zone_id")
            break
    return dict(cell_id=int(rc[0] * shape[1] + rc[1]) if rc else None, zone_id=zone, engine="python")


def _point_in_ring(x, y, ring):
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1:
            inside = not inside
    return inside


# ---- feature store ---------------------------------------------------------------------------
def fs_put(name, df, description):
    FEATURE_STORE.mkdir(parents=True, exist_ok=True)
    df.to_csv(FEATURE_STORE / f"{name}.csv", index=False)
    meta = dict(name=name, description=description, rows=len(df), columns=list(df.columns),
                version=hashlib.sha1(pd.util.hash_pandas_object(df, index=False).values.tobytes()).hexdigest()[:12],
                created=datetime.now().isoformat(timespec="seconds"))
    (FEATURE_STORE / f"{name}.meta.json").write_text(json.dumps(meta, indent=2))
    return meta


def fs_get(name):
    return pd.read_csv(FEATURE_STORE / f"{name}.csv")


def inventory():
    """Sizes and counts for every store, for the System page."""
    def files(root):
        return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []

    lake = files(LAKE)
    out = dict(
        lake=dict(files=len(lake), bytes=sum(p.stat().st_size for p in lake),
                  sources=sorted({p.parent.name for p in lake})),
        warehouse=dict(bytes=_db.size_bytes("warehouse"), tables={}),
        geodb=dict(bytes=_db.size_bytes("geo"), tables={}),
        feature_store=[json.loads(p.read_text()) for p in sorted(FEATURE_STORE.glob("*.meta.json"))],
        database=_db.describe(),
    )
    for key, store in (("warehouse", "warehouse"), ("geodb", "geo")):
        ts = _db.tables(store)
        if ts:
            with _db.connect(store) as c:
                for t in ts:
                    out[key]["tables"][t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return out


def reset():
    """Drop the rebuildable stores (warehouse, geo); persistent stores are untouched."""
    DATA.mkdir(parents=True, exist_ok=True)
    if _db.is_pg():
        for store in ("warehouse", "geo"):
            ts = _db.tables(store)
            with _db.connect(store) as c:
                for t in ts:
                    c.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
        return
    for p in (WAREHOUSE, GEODB):
        if p.exists():
            p.unlink()
