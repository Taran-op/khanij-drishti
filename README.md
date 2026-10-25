# KhanijDrishti

**AI/ML and space technology for manganese reserve identification and production shortfall prediction**

Area of interest: the Balaghat · Nagpur · Bhandara manganese belt (Sausar Group), central India.
Six producing mines, 12,000 grid cells at 0.01° (~1.1 km).

The system answers two questions:

1. **Where is the next manganese?** Satellite, terrain, geology, geochemistry and geophysics go into an ML prospectivity map. The map yields probable reserve zones, each with a confidence score, an indicative grade and tonnage, and a drill-down that explains the estimate.
2. **Will we hit this month's target, and what do we do if not?** Equipment, fleet, manpower, blasting and weather feed a P10/P50/P90 forecast. From that forecast the system gives a month-end risk, an early warning, what-if analysis, and a budget-constrained corrective-action plan.

---

## Run it

Requirements: Python 3.11+ and Node 20+.

```bash
# backend
cd backend
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m kd.pipeline             # builds data lake → warehouse → models → alerts (~60 s)
uvicorn kd.api:app --port 8000

# frontend (second terminal)
cd frontend
npm install
npm run dev                       # http://localhost:5173
```

**Email/SMS without real credentials:** run `python -m kd.notify sink` (an SMTP sink on 127.0.0.1:1025 that stores .eml files), and start the API with `KD_DEV_SINKS=1` (a Twilio-compatible SMS sink at `http://127.0.0.1:8000/dev/sms`). Then point Delivery settings at them.

**Single-process demo:** run `npm run build` in `frontend/`. After that, `uvicorn kd.api:app --port 8000` serves both the API and the UI at <http://localhost:8000>.

If `backend/data` is missing, the API runs the pipeline on first start.
To rebuild everything from the UI, use **Data & Models → Re-run full pipeline**.

> Map basemaps (Esri imagery, OpenTopoMap, OSM) need internet. All model layers are drawn locally. Pick the **None (offline)** basemap if the venue Wi-Fi is bad.

---

## Architecture → code

| Layer (from the diagram) | Where | What it does |
|---|---|---|
| **1 · Data Sources** | `backend/kd/sources.py` | Writes raw feeds to the data lake. Satellite data: Sentinel-2 Mn ratio and NDVI, Sentinel-1 VV, SRTM DEM, MODIS LST, IMD rainfall and SMAP soil moisture on a coarse 0.25° grid. Geology data: GSI units, faults, 260 boreholes, 450 stream-sediment samples, aeromagnetic flight lines. Production data: shift MIS, equipment telemetry, blasting register, attendance, monthly plan. External data: IMD station rain, the 30-day extended-range forecast, and regulatory limits. |
| **2 · Ingestion & Processing** | `backend/kd/processing.py` | Ingestion log with per-source channel. Cloud-gap inpainting and resampling of coarse grids to the analysis grid. Slope and hillshade. Spatial-context (CNN-receptive-field) features. Geochem interpolation (IDW) and gridding of the aeromagnetic flight lines. A **leakage guard** masks active pits. MIS cleaning removes duplicates, −1 sentinels and kg-for-tonne unit errors, and fills gaps. Spatio-temporal joins link mines to their nearest weather station. Feature engineering is shared between training and serving. |
| **3 · Data Storage** | `backend/kd/storage.py` | `data/lake/raw` (data lake), `warehouse.sqlite` (processed warehouse), `geodb.sqlite` (grid cells with WKT polygons plus vector layers, a PostGIS stand-in), and `feature_store/` (versioned feature sets with hashes). |
| **4 · AI/ML: reserves** | `backend/kd/reserve.py`, `kd/unet.py` | Four candidates on real inputs only: **random forest**, **XGBoost**, a **U-Net** (PyTorch) on a 10-band real Sentinel-2 + DEM stack at 0.002°, and their **ensemble**. All use the same MRDS/MOIL labels, the same **spatial block CV**, and the same held-out test on USGS occurrences that were never labels. The champion (best generalisation score) drives the map. Confidence comes from fold-model stability. Zones are connected components with a convex hull. Ridge models estimate grade and thickness from borehole intercepts. Per-cell explanations come from the random forest. |
| **4 · AI/ML: shortfall** | `backend/kd/shortfall.py`, `kd/forecasters.py` | Three forecaster families on one contract: a **gradient-boosting** driver model, a **seq2seq LSTM** (PyTorch: a 28-day encoder plus a decoder over future drivers, pinball loss) and **Prophet** per mine with driver regressors. All give P10/P50/P90. They share one scenario builder: forecast rain, mean-reverting equipment, festival calendar and monsoon blasting limits. A 6-origin rolling backtest scores every family against a seasonal-naive baseline, and the lowest 30-day MAPE serves. Outputs: month-end miss probability, expected shortfall and driver attribution. |
| **4 · Model registry** | `backend/kd/registry.py` | Every candidate is versioned under `models/registry/<task>/<version>` with its metrics and data fingerprint. Each task has one serving champion. Admins can promote, roll back or pin a family via `/api/models` or **Data & Models → Model registry**. Every action is audited. |
| **4 · Optimization** | `backend/kd/optimize.py` | What-if on any driver. Greedy knapsack over six corrective actions (maintenance, dumper re-deployment, overtime, blasting, dewatering, face re-sequencing). Every candidate is re-scored by the forecaster. Constraints: budget, the monsoon blasting cap, the EC annual ceiling, and donor mines staying on target. |
| **5 · Application** | `backend/kd/api.py`, `frontend/` | FastAPI REST API. React dashboard, Leaflet GIS viewer (18 layers, overlays, drill-down, cross-section), production charts, what-if sliders, alerts with an Email/SMS outbox, CSV export, and print-to-PDF. |
| **6 · Users & Actions** | `backend/kd/auth.py`, login page | Sign-in with role-based access. Planners and geologists start on the Reserve Explorer, the operations team on Production, management on the Command Center, and admins on Data & Models. |
| Alerts | `backend/kd/alerts.py` | Shortfall risk, breakdowns, equipment degradation trend, heavy-rain forecast at opencast pits, festival manpower dips, and new reserve zones. Alerts fan out to recipients by audience and minimum severity. |
| **5 · Notifications** | `backend/kd/notify.py` | Real **SMTP email** and **Twilio-compatible SMS**. Configured by an admin under Alerts & Reports → Delivery settings; secrets can come from `KD_SMTP_PASSWORD` / `KD_SMS_TOKEN`. A persistent outbox retries up to 3× and de-duplicates for 24 h. Unconfigured channels hold their messages. |
| **5 · Reports** | `backend/kd/reports.py`, `kd/scheduler.py` | Three reports: daily operations brief, management summary, and reserve & exploration report. Each renders as **PDF** (reportlab with charts and a map) and **Excel** (formatted sheets with native charts). They are archived and can be downloaded. The built-in scheduler generates and emails them daily, weekly or monthly, and runs the daily data refresh. |
| **5 · 3D GIS viewer** | `backend/kd/terrain.py`, `frontend/src/components/Terrain3D.jsx` | three.js terrain built from the real Copernicus DEM at 0.002°, with adjustable vertical exaggeration. Drapes are real Sentinel-2 true colour, a SWIR geology composite, or NDVI. Any model or data layer can be blended on top, with zones, rivers and mine pins. Click to drill down. |
| **5 · Equipment status** | `kd/ingest.py` `equipment_status`, `LiveEquipment.jsx` | Per-machine online/offline status from heartbeats, 24 h status timelines, live availability and utilisation, and the 30-day daily log. The virtual-mine live feed can run as an API background service (`KD_LIVE_FEED=1` or the Start button). |

---

## Current results (real inputs, data through 2026-10-11)

**Reserve prospectivity.** Every candidate uses real inputs only, with MRDS/MOIL labels and 50 held-out USGS occurrences.

| Candidate | Spatial-CV AUC | Held-out occurrences >6 km, AUC | Generalisation score |
|---|---|---|---|
| Random forest | 0.618 | 0.956 | 0.787 |
| XGBoost | 0.659 | 0.927 | 0.793 |
| U-Net (Sentinel-2 + DEM, 0.002°) | **0.847** | 0.823 | 0.835 |
| **Ensemble (serving)** | 0.777 | 0.952 | **0.865** |

The model produced 11 compact zones, all brownfield extensions around recorded workings. They total ~147 Mt (indicative only, G4, because it uses simulated boreholes). No greenfield zone passes the threshold. The models rank distant occurrences well but stay conservative far from known mines.

**Production shortfall.** Six rolling origins, Apr–Sep 2026, with drivers frozen at the origin.

| Forecaster | 30-day MAPE | Daily MAE |
|---|---|---|
| **Gradient boosting (serving)** | **11.3%** | **106 t** |
| LSTM (seq2seq) | 12.2% | 109 t |
| Prophet + regressors | 13.0% | 110 t |
| Seasonal naive (baseline) | 11.4% | 136 t |

- Every learned model beats the baseline on daily error by about 20%.
- On 30-day totals only the GBM edges it.
- September 2026 is hard for all of them (20–24% error), because MOIL's published output dropped sharply that month.
- The daily P10–P90 band is conformally calibrated. Coverage on held-out days rose from 28% (raw quantile heads) to 64%, against a nominal 80%. The error regime is still shifting.

## Sign-in and role-based access

Every API route except `/api/health` requires sign-in, and each route checks the permission it needs **on the server**. Hiding a button in the UI is only a convenience; the API is what enforces access.

| Permission | Admin | Management | Planner / Geologist | Operations |
|---|:-:|:-:|:-:|:-:|
| Command Center | ● | ● | ● | ● |
| Reserve maps, GIS layers, reserve exports | ● | ● | ● | – (overview map only) |
| Production & forecasts | ● | ● | ● | ● |
| What-if analysis | ● | ● | ● | ● |
| Generate corrective-action plans | ● | ● | – | ● |
| Alerts (only those addressed to the role) | all | ● | ● | ● |
| Acknowledge alerts | ● | – | – | ● |
| Data & Models | ● | ● | ● | – |
| Re-run pipeline, users & audit log | ● | – | – | – |

- **Passwords:** PBKDF2-SHA256 with 200k iterations and a per-user salt, stored in `data/auth.sqlite`. A pipeline rebuild never touches this file.
- **Sessions:** signed (HMAC-SHA256) tokens that expire after 12 hours. The role is re-read on every request, so a role change takes effect immediately.
- **Brute-force guard:** 5 failed attempts in 5 minutes lock the username for the rest of that window.
- **Audit log:** every sign-in, failure, lockout and sign-out is recorded. Admins can view it under Data & Models.
- **Demo accounts:** listed in `backend/kd/seed_users.py` and shown as one-click buttons on the login page. For a real deployment, set `KD_DEMO_LOGIN=0` to hide them and `KD_TOKEN_SECRET` to at least 32 random bytes. Then delete `data/auth.sqlite` and seed real accounts.

---

## About the data

The project is moving from simulated to real data, phase by phase (see [ROADMAP.md](ROADMAP.md)). Every layer in the app carries a **REAL** or **SIMULATED** badge, and **Data & Models → Data provenance** lists each real feed with its source, scene IDs, dates and licence.

**Real today** (fetched by `kd/eo.py`, no accounts needed):

| Data | Source |
|---|---|
| Sentinel-2 L2A dry-season composite: NDVI, Fe–Mn oxide index, ferric and clay ratios | Microsoft Planetary Computer |
| Sentinel-1 RTC VV/VH backscatter | Microsoft Planetary Computer |
| Copernicus DEM GLO-30: elevation, slope, hillshade | Microsoft Planetary Computer |
| MODIS MOD11A2 land-surface temperature | Microsoft Planetary Computer |
| Daily rain + soil moisture at every mine since 2023, annual rainfall grid | ERA5 / ERA5-Land via Open-Meteo |
| 16-day rain forecast per mine (climatology for days 17–30) | Open-Meteo forecast |
| Mine and deposit locations | OpenStreetMap (mapped MOIL quarries + village positions) |

| Recorded manganese sites (79; 30 mines used as labels, 50 occurrences held out for testing) | USGS MRDS |
| Magnetic anomaly | NOAA EMAG2 v3 |
| Lithology | Macrostrat (global scale) |
| Lineaments | derived from the Copernicus DEM |
| Rivers | OpenStreetMap |

**Uploads:** Data & Models → Data uploads accepts GSI/contractor boreholes, geochemistry, aeromagnetic grids and geology maps. They are validated, versioned and replace the open data on the next pipeline run.

**Still simulated** (by `kd/sources.py`), each with a replacement planned:
- **Boreholes, stream-sediment geochemistry:** no public source. Upload real files to replace them. Until then the reserve model does not use them, and grade and tonnage are marked indicative.
- **Mine operations (equipment, blasting, manpower, fleet, daily tonnes):** no public source exists. These records enter **only through the ingestion API** (`POST /api/ingest/telemetry`, device keys, validation, dead-letter queue). A separate **virtual mine** service plays the field devices. Its output responds to real ERA5 rainfall and is calibrated so the six mines deliver 70% of **MOIL's published company production** in every reported period (`kd/moil_public.py` cites each figure). The per-mine split is an assumption, and the app labels it as one.

```bash
python -m kd.virtual_mine provision                                 # one API key per mine
python -m kd.virtual_mine live --http http://127.0.0.1:8000         # heartbeats + shift tonnes, every 20 s
```

A real mine connects the same way: an admin issues a device key (Data & Models → Operational ingestion), and the mine's fleet-management or MIS system posts to the API.

The reserve model trains on real inputs only (100% real decision weight). Its card shows a held-out test on 50 real USGS occurrences, split by distance from the training mines.

`python -m kd.pipeline` re-uses cached real downloads. Add `--refresh-eo` to re-fetch the satellite composites. Weather and forecast refresh automatically each day.

---

## Production infrastructure

Copy `.env.example` to `.env`, replace every placeholder, and run `docker compose up -d`. The production topology is:

- PostgreSQL 16 + PostGIS for warehouse, spatial, auth, telemetry, registry, queue and audit stores;
- independent FastAPI, worker and scheduler services, with an optional `simulator` profile;
- nginx-hosted React UI, Prometheus metrics and a provisioned Grafana operations dashboard;
- nightly checksummed backups, weekly restore drills, model/feature drift and readiness probes.

SQLite remains the zero-setup local-development fallback. `python -m kd.migrate import-sqlite data` performs a one-time
production import and builds PostGIS geometry/GIST indexes. The Data & Models page surfaces platform health, drift,
background jobs, migrations and disaster-recovery state.

See [the deployment guide](deploy/DEPLOYMENT.md) for Docker, Kubernetes, AWS/Azure/GCP mappings, TLS, secrets,
managed-PostGIS recommendations and the restore runbook. Kubernetes templates are in `deploy/k8s/`.

---

## Demo script (5 minutes)

1. **Command Center:** October is projected about 21 kt short. Three mines are at high risk. Seven new reserve zones have been found.
2. **Reserve Explorer:** switch layers (Mn index → NDVI → geology → prospectivity). Click **NZ-02**, look at the explanation bars and the cross-section, then the Model tab: spatial-CV AUC.
3. **Production → DGB:** the breakdown dip, a heavy-rain spell forecast for 18–21 Oct, and driver attribution showing equipment as the biggest lever.
4. **Optimization:** the plan at ₹40 lakh. Drag the budget, then use the what-if sliders: excavator availability to 92% from day 3, and watch the risk fall.
5. **Alerts & Reports:** alerts with recommended actions, the SMS/Email outbox, and CSV export.
6. **Data & Models:** the six layers, live, with pipeline logs, feature-store versions and the backtest table.
