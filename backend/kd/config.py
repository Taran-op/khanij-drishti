"""Central configuration: paths, area of interest, mines, and the 'as of' date."""
import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("KD_DATA_DIR", ROOT / "data"))   # a volume in containers
LAKE = DATA / "lake" / "raw"          # Layer 3 · Data Lake (raw, as ingested)
WAREHOUSE = DATA / "warehouse.sqlite"  # Layer 3 · Processed Data Warehouse
GEODB = DATA / "geodb.sqlite"          # Layer 3 · Geospatial Database (PostGIS when KD_DATABASE_URL is set)
FEATURE_STORE = DATA / "feature_store" # Layer 3 · Model Feature Store
MODELS = Path(os.environ.get("KD_MODELS_DIR", ROOT / "models"))
REGISTRY = DATA / "registry.sqlite"     # uploads registry: survives pipeline rebuilds (unlike the warehouse)

SEED = int(os.environ.get("KD_SEED", 42))

# Forecast origin = today (real data is fetched up to yesterday). Pin with KD_ASOF=YYYY-MM-DD for a reproducible demo.
AS_OF = date.fromisoformat(os.environ.get("KD_ASOF", str(date.today())))
HISTORY_START = date(2023, 1, 1)
HORIZON_DAYS = 30

# Area of interest: Balaghat - Nagpur - Bhandara manganese belt (Sausar Group), central India.
BOUNDS = dict(lat_min=21.25, lat_max=22.05, lon_min=79.00, lon_max=80.50)
RES = 0.01  # degrees (~1.1 km)

# Producing MOIL mines. Locations verified against OpenStreetMap on 2026-10-12:
#   osm   = the mapped mine/quarry polygon itself      village = the mine's village (Nominatim)
# Capacities, dumpers and EC caps are ILLUSTRATIVE until real figures are ingested (see ROADMAP.md).
MINES = [
    dict(id="BLG", name="Balaghat (Bharveli)", district="Balaghat", state="MP", lat=21.8502, lon=80.2274, loc_src="osm",
         method="underground", capacity_tpd=1400, dumpers=14, ec_cap_tpa=600_000),
    dict(id="UKW", name="Ukwa", district="Balaghat", state="MP", lat=21.9708, lon=80.4666, loc_src="village",
         method="underground", capacity_tpd=500, dumpers=6, ec_cap_tpa=215_000),
    dict(id="TRD", name="Tirodi", district="Balaghat", state="MP", lat=21.6835, lon=79.7246, loc_src="osm",
         method="opencast", capacity_tpd=450, dumpers=8, ec_cap_tpa=195_000),
    dict(id="DGB", name="Dongri Buzurg", district="Bhandara", state="MH", lat=21.5502, lon=79.6940, loc_src="village",
         method="opencast", capacity_tpd=900, dumpers=12, ec_cap_tpa=390_000),
    dict(id="CHK", name="Chikla", district="Bhandara", state="MH", lat=21.5385, lon=79.7523, loc_src="osm",
         method="underground", capacity_tpd=350, dumpers=5, ec_cap_tpa=150_000),
    dict(id="KDR", name="Kandri", district="Nagpur", state="MH", lat=21.4205, lon=79.2764, loc_src="village",
         method="underground", capacity_tpd=300, dumpers=4, ec_cap_tpa=130_000),
]
MINE_BY_ID = {m["id"]: m for m in MINES}

# Known deposits used as positive training labels: (name, lat, lon, location source).
# Gumgaon lies outside the study area (20.99°N) and is excluded. Beldongri and Sitapatore could
# not be verified on OSM and keep their earlier approximate positions, flagged "unverified".
KNOWN_DEPOSITS = [(m["name"], m["lat"], m["lon"], m["loc_src"]) for m in MINES] + [
    ("Munsar (Mansar)", 21.3975, 79.2700, "osm"),
    ("Beldongri", 21.400, 79.360, "unverified"),
    ("Sitapatore", 21.700, 79.650, "unverified"),
]

# Festival days that depress manpower attendance (subset, for the calendar feature).
FESTIVALS = {
    "2023-03-08", "2023-10-24", "2023-11-12", "2023-11-13",
    "2024-03-25", "2024-10-12", "2024-10-31", "2024-11-01",
    "2025-03-14", "2025-10-02", "2025-10-20", "2025-10-21",
    "2026-03-04", "2026-10-20", "2026-11-08", "2026-11-09",
}
