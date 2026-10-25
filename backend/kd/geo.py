"""Small geospatial helpers: the analysis grid, smooth random fields, distances."""
import numpy as np

from .config import BOUNDS, RES

KM_PER_DEG_LAT = 110.57


def km_per_deg_lon(lat):
    return 111.32 * np.cos(np.radians(lat))


def grid():
    """Cell-centre coordinates. Row 0 is the northern edge (image convention)."""
    lats = np.arange(BOUNDS["lat_max"] - RES / 2, BOUNDS["lat_min"], -RES)
    lons = np.arange(BOUNDS["lon_min"] + RES / 2, BOUNDS["lon_max"], RES)
    lon2d, lat2d = np.meshgrid(lons, lats)
    return lat2d, lon2d


def cell_of(lat, lon):
    lat2d, _ = grid()
    rows, cols = lat2d.shape
    r = int((BOUNDS["lat_max"] - lat) / RES)
    c = int((lon - BOUNDS["lon_min"]) / RES)
    if 0 <= r < rows and 0 <= c < cols:
        return r, c
    return None


def smooth_field(shape, scale_cells, rng):
    """Gaussian-filtered white noise in the frequency domain, normalised to [0, 1]."""
    noise = rng.standard_normal(shape)
    fy = np.fft.fftfreq(shape[0])[:, None]
    fx = np.fft.fftfreq(shape[1])[None, :]
    kernel = np.exp(-2 * (np.pi * scale_cells) ** 2 * (fx ** 2 + fy ** 2))
    f = np.real(np.fft.ifft2(np.fft.fft2(noise) * kernel))
    return (f - f.min()) / (f.max() - f.min() + 1e-12)


def dist_km_to_points(lat2d, lon2d, pts):
    """Distance (km) from every cell to the nearest of pts [(lat, lon), ...]."""
    d = np.full(lat2d.shape, np.inf)
    for la, lo in pts:
        dy = (lat2d - la) * KM_PER_DEG_LAT
        dx = (lon2d - lo) * km_per_deg_lon(la)
        d = np.minimum(d, np.hypot(dx, dy))
    return d


def dist_km_to_polyline(lat2d, lon2d, line):
    """Distance (km) and along-line position (km) from every cell to a polyline [(lon, lat), ...]."""
    kx = km_per_deg_lon(np.mean([p[1] for p in line]))
    px, py = lon2d * kx, lat2d * KM_PER_DEG_LAT
    best = np.full(lat2d.shape, np.inf)
    along = np.zeros(lat2d.shape)
    run = 0.0
    for (lo1, la1), (lo2, la2) in zip(line[:-1], line[1:]):
        ax, ay, bx, by = lo1 * kx, la1 * KM_PER_DEG_LAT, lo2 * kx, la2 * KM_PER_DEG_LAT
        vx, vy = bx - ax, by - ay
        seg = np.hypot(vx, vy)
        t = np.clip(((px - ax) * vx + (py - ay) * vy) / (seg ** 2), 0, 1)
        d = np.hypot(px - (ax + t * vx), py - (ay + t * vy))
        closer = d < best
        best = np.where(closer, d, best)
        along = np.where(closer, run + t * seg, along)
        run += seg
    return best, along


def resample_coarse(arr, coarse_bounds, coarse_res):
    """Bilinearly resample a coarse raster (north-up) onto the analysis grid."""
    lat2d, lon2d = grid()
    rows, cols = arr.shape
    fr = (coarse_bounds["lat_max"] - lat2d) / coarse_res - 0.5
    fc = (lon2d - coarse_bounds["lon_min"]) / coarse_res - 0.5
    fr = np.clip(fr, 0, rows - 1)
    fc = np.clip(fc, 0, cols - 1)
    r0, c0 = np.floor(fr).astype(int), np.floor(fc).astype(int)
    r1, c1 = np.minimum(r0 + 1, rows - 1), np.minimum(c0 + 1, cols - 1)
    wr, wc = fr - r0, fc - c0
    return (arr[r0, c0] * (1 - wr) * (1 - wc) + arr[r1, c0] * wr * (1 - wc)
            + arr[r0, c1] * (1 - wr) * wc + arr[r1, c1] * wr * wc)
