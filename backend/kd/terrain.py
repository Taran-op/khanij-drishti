"""Layer 5 · 3D GIS viewer data: real terrain mesh and image drapes.

  elevation   Copernicus DEM GLO-30 resampled to 0.002° (from the U-Net stack, kd/eo.py), served
              as little-endian int16 metres, optionally decimated for the browser mesh
  textures    rendered from real Sentinel-2 L2A dry-season medians at the same resolution:
                rgb   true colour (B4/B3/B2, 2–98 % stretch, gamma)
                swir  SWIR geology composite (B12/B8A/B4): Mn/Fe-oxide and clay-rich ground stands out
                ndvi  vegetation index (brown → green)
              model and data layers on the 0.01° grid are coloured client side (same ramps as the 2D map)
"""
import io
import json

import numpy as np

from .config import BOUNDS, LAKE

_cache = {}


def _stack():
    if "stack" not in _cache:
        z = np.load(LAKE / "satellite" / "unet_stack.npz")
        names = json.loads(str(z["meta"]))["bands"]
        _cache["stack"] = (z["data"], {n: i for i, n in enumerate(names)})
        prov = LAKE / "satellite" / "unet_stack.provenance.json"
        _cache["prov"] = json.loads(prov.read_text()) if prov.exists() else {}
    return _cache["stack"]


def available():
    return (LAKE / "satellite" / "unet_stack.npz").exists()


def meta(step=2):
    d, ix = _stack()
    e = d[ix["elevation"]][::step, ::step]
    p = _cache.get("prov", {})
    return dict(bounds=BOUNDS, width=int(e.shape[1]), height=int(e.shape[0]), step=step, full=[int(d.shape[2]), int(d.shape[1])],
                elev_min=float(np.nanmin(e)), elev_max=float(np.nanmax(e)), units="m",
                km=dict(x=(BOUNDS["lon_max"] - BOUNDS["lon_min"]) * 103.4, y=(BOUNDS["lat_max"] - BOUNDS["lat_min"]) * 110.57),
                textures=["rgb", "swir", "ndvi"],
                source=dict(dem="Copernicus DEM GLO-30 (Planetary Computer)", imagery=p.get("source", "Sentinel-2 L2A (Planetary Computer)"),
                            dates=p.get("date_range"), scenes=len(p.get("scenes", [])) or None))


def elevation_bytes(step=2):
    d, ix = _stack()
    e = d[ix["elevation"]][::step, ::step]
    e = np.where(np.isfinite(e), e, np.nanmedian(e))
    return np.round(e).astype("<i2").tobytes()


def _stretch(b, gamma=1.0):
    lo, hi = np.nanpercentile(b, [2, 98])
    x = np.clip((b - lo) / (hi - lo + 1e-9), 0, 1)
    return np.nan_to_num(x ** gamma)


def texture_png(kind):
    from PIL import Image
    key = f"tex-{kind}"
    if key not in _cache:
        d, ix = _stack()
        if kind == "rgb":
            img = np.stack([_stretch(d[ix[b]], 0.85) for b in ("B04", "B03", "B02")], -1)
        elif kind == "swir":
            img = np.stack([_stretch(d[ix[b]]) for b in ("B12", "B8A", "B04")], -1)
        elif kind == "ndvi":
            v = np.clip((np.nan_to_num(d[ix["NDVI"]]) + 0.1) / 0.8, 0, 1)
            stops = np.array([[140, 98, 57], [200, 170, 110], [235, 225, 150], [130, 190, 90], [40, 130, 60], [10, 80, 40]], float) / 255
            pos = v * (len(stops) - 1)
            i = np.minimum(pos.astype(int), len(stops) - 2)
            f = (pos - i)[..., None]
            img = stops[i] * (1 - f) + stops[i + 1] * f
        else:
            raise KeyError(kind)
        buf = io.BytesIO()
        Image.fromarray((img * 255).astype("uint8")).save(buf, format="PNG", optimize=True)
        _cache[key] = buf.getvalue()
    return _cache[key]


def clear():
    _cache.clear()
