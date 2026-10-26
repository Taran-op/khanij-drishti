# KhanijSetu: road to "every box in the architecture, on real data"

Decisions (2026-10-12):
- **Mine operational data:** real ingestion APIs (IoT/HTTP + CSV/Excel upload), fed by a separate *virtual mine* telemetry service calibrated to MOIL's published production. A real mine later just points its feed at the API.
- **Geology:** open global datasets now, plus an upload pipeline so GSI Bhukosh files replace them when downloaded.
- **Infra:** Docker Desktop → Docker Compose (PostGIS, API, worker, scheduler, monitoring, backups) + Kubernetes manifests.

What is honestly *not* available publicly anywhere: per-mine daily equipment, blasting, manpower and fleet logs; borehole logs. These come through the ingestion APIs (virtual mine now, real mine later) and the upload pipeline.

## Phase 1: Real Earth-observation data (Microsoft Planetary Computer, Open-Meteo, NASA POWER, OSM) ✅ 2026-10-12
Result: 13 real lake datasets; shortfall model 8.7% MAPE vs 11.4% baseline on real weather; reserve model real-data weight 26%
(the remaining 74% is simulated geology, which Phase 2 replaces). Landsat cross-check deferred (Sentinel-2 + MODIS cover it).
- [x] Sentinel-2 L2A cloud-masked median composite → Mn-oxide ratio, NDVI, false colour
- [ ] Landsat 8/9 → second optical source and thermal LST cross-check
- [x] Sentinel-1 RTC → VV/VH backscatter
- [x] Copernicus DEM GLO-30 → elevation, slope, hillshade
- [x] MODIS LST (MOD11A2) → day land-surface temperature
- [x] Soil moisture + rainfall history (NASA POWER / ERA5 via Open-Meteo archive), per mine and gridded
- [x] Weather forecast (Open-Meteo, 16 days) replacing the synthetic IMD forecast
- [x] Real mine and deposit locations (OSM + MOIL mine list)
- [x] Data-lake provenance: every raster/table records source, scene IDs, dates, licence

## Phase 2: Real geology / geophysics ✅ 2026-10-12
Result: the reserve model now trains on 100% real inputs. Labels = 30 real mines (USGS MRDS producers/past producers +
OSM-verified MOIL mines); 50 MRDS occurrences held out. Spatial-block CV AUC 0.62 (honest: the earlier 0.95 leaned on
simulated geology built from the same horizons as the labels). Held-out occurrences: AUC 0.975 overall, 0.91 for
occurrences >10 km from any training mine; 60% fall inside flagged zones vs 0.9% of background.
- [x] USGS MRDS manganese records (79; producers/past producers → labels, occurrences → held-out test)
- [x] EMAG2 v3 magnetic anomaly grid (NOAA ImageServer subset)
- [x] Lithology: Macrostrat global units on a 0.05° lattice (GLiM/USGS South Asia unavailable; GSI via upload)
- [x] Lineaments derived from the real Copernicus DEM (replaces invented faults)
- [x] Rivers from OpenStreetMap
- [x] Upload pipeline + UI: boreholes, geochem (CSV/XLSX), aeromag (GeoTIFF), geology (shapefile ZIP / GeoJSON); validated,
      versioned in a persistent registry, override open/simulated data on the next run; role permission `data_upload`
- Still no public source: borehole logs, stream-sediment geochemistry → upload when obtained (GSI NGCM / drilling contractors)

## Phase 3: Real ingestion + virtual mine ✅ 2026-10-12
Result: mine operations reach the platform ONLY through the ingestion API. 49.9k records backfilled, 102 dead-lettered by
validation; six-mine output = 70.0% of MOIL's published company production in every reported period (FY24 → Sep 2026).
Forecaster on ingested data: daily MAE 119 t vs 142 t seasonal-naive; 30-day MAPE 13.2% vs 13.9%.
- [x] `POST /api/ingest/telemetry` with per-device API keys (hashed; a key may only write its own mine), pydantic schemas
      for production / equipment / blasting / manpower / plan / heartbeat, idempotent upserts, dead-letter queue, JSONL
      batch archive in the data lake, persistent operational store `data/ops.sqlite`
- [x] MIS CSV/XLSX upload ("Mine operations") through the same validation path
- [x] Virtual mine (`python -m kd.virtual_mine provision | backfill | live`): device keys, history backfill, live
      heartbeats + shift tonnes + daily logs; driven by real ERA5 rain; calibrated to MOIL published totals
      (kd/moil_public.py, 8 cited sources); per-mine split + 70% share + plan growth are labelled assumptions
- [x] Pipeline reads operations from the ingestion store (no in-process generation); provenance says virtual vs real
- [x] Live equipment panel (Production page) + Operational ingestion & calibration panels (Data & Models); admin device keys
- Not public anywhere: per-mine split, daily logs. A real mine connects by getting a device key and posting to the API.

## Phase 4: The diagram's model set ✅
- [x] U-Net (PyTorch) segmentation on real Sentinel-2 patches, labelled from known deposits/occurrences
      (`kd/unet.py`: 10-band 0.002° stack, weak pixel labels, masked BCE, 5-fold spatial-block CV; re-used until imagery/labels change, `KD_TRAIN_DL=1` forces retraining)
- [x] Random Forest + XGBoost prospectivity (compare, spatial CV) + ensemble; all scored on spatial CV and held-out USGS occurrences;
      champion = best generalisation score (mean of CV AUC and far-occurrence AUC)
- [x] LSTM and Prophet production forecasters alongside GBM; backtest leaderboard (`kd/forecasters.py`, 6 rolling origins, same driver scenario, seasonal-naive baseline)
- [x] Model registry: versions, metrics, data fingerprint, champion, promote/rollback/pin (`kd/registry.py`, `/api/models`, Data & Models → Model registry; admin `models` permission, audited)

## Phase 5: Application features ✅
- [x] Periodic reports (scheduled), real Email (SMTP) / SMS (Twilio-compatible) with config
      (`kd/scheduler.py` daily/weekly/monthly jobs with compare-and-set claiming; `kd/notify.py` outbox with retries,
      24 h de-duplication, audience + severity routing; admin settings/recipients/test on Alerts & Reports; dev sinks)
- [x] PDF and Excel (.xlsx) export (`kd/reports.py`: ops brief, management summary, reserve report; reportlab +
      matplotlib charts and map; openpyxl sheets with native charts; archive + download)
- [x] 3D GIS viewer (terrain + prospectivity drape) (`kd/terrain.py` + three.js: Copernicus DEM, Sentinel-2
      true colour / SWIR / NDVI drapes, any model layer, zones, pins, click-to-drill-down)
- [x] Equipment status panel from live telemetry (per-machine online/offline, 24 h status timelines, 30-day log;
      virtual-mine live feed as an API background service)

## Phase 6: Security, infrastructure, deployment ✅ 2026-10-25
Result: the same application now runs on SQLite for local development or PostgreSQL/PostGIS in production. A clean
PostGIS 16 rehearsal applied all migrations, imported 16 warehouse tables and 12,000 spatial cells, rebuilt geometry
indexes and returned the expected zone through `ST_Contains`. The SQLite backup/restore drill passed checksum and
integrity verification (97 files, 36 tables), and a disposable PostgreSQL `pg_dump` restored and verified all 20 tables.
- [x] PostgreSQL + PostGIS (warehouse, geodb, auth), versioned and serialised migrations; SQLite → PostgreSQL importer
- [x] Docker Compose: api, worker, scheduler, optional virtual-mine, postgis, nginx/frontend, prometheus, grafana
- [x] Kubernetes manifests + cloud deploy guide (AWS/Azure/GCP), TLS ingress, HPA, PDB, resource/security contexts
- [x] Monitoring: `/metrics`, `/health`, `/ready`, live forecast accuracy, input PSI drift, queue/ingest/backup metrics,
      provisioned Prometheus/Grafana dashboard and alert rules
- [x] Nightly backups + weekly restore drill; TLS and external-secret guidance; mandatory env secrets; exact-origin CORS
