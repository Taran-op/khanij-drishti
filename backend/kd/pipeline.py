"""Runs the whole architecture end to end: Layer 1 -> 4, then alerts.

    python -m kd.pipeline            # full rebuild (simulate sources, process, train, alert)
    python -m kd.pipeline --no-sim   # keep the lake as-is (e.g. after dropping in real files)
"""
import sys
import time

import os

from . import alerts, eo, geology_real, ingest, processing, reserve, shortfall, sources, storage, virtual_mine
from .config import LAKE


def run(simulate=True, log=print, refresh_eo=False):
    t0 = time.time()
    storage.reset()
    shortfall.reload()
    log("[1/5] Layer 1 · REAL sources: Sentinel-2/1, Copernicus DEM, MODIS, ERA5, forecast (cached)")
    real = eo.fetch_all(refresh=refresh_eo, log=log)
    log("      REAL geology: USGS MRDS, EMAG2, Macrostrat, DEM lineaments, OSM rivers (cached)")
    real.update(geology_real.fetch_all(refresh=refresh_eo, log=log))
    storage.log_run(1, "real data sources", **{k: ("ok" if v["ok"] else "FAILED") for k, v in real.items()})
    if simulate or not LAKE.exists():
        log("      simulated feeds for everything not yet real (geology, boreholes, mine operations)")
        sources.generate_all()
        storage.log_run(1, "simulated data sources", lake=str(LAKE))
    # Mine operations come only through the ingestion API's store. In demo mode the virtual mine
    # catches the store up to yesterday (same validation path a real device uses).
    if os.environ.get("KD_VIRTUAL_MINE", "1") == "1":
        vm = virtual_mine.backfill(log=log)
        storage.log_run(1, "virtual-mine backfill", **vm)
    snap = ingest.snapshot_to_lake()
    log(f"      operations from ingestion store: {snap}")
    storage.log_run(2, "ops store snapshot", **snap)
    log("[2/5] Layer 2-3 · ingest, clean, geoprocess, integrate, feature store")
    processing.run()
    log("[3/5] Layer 4 · reserve identification & mapping")
    card, zones = reserve.train_and_map()
    log(f"      spatial-CV ROC-AUC {card['roc_auc']}, {sum(z['status'] == 'new' for z in zones)} new zones")
    log("[4/5] Layer 4 · production shortfall model + backtest")
    sc = shortfall.train(log=log)
    shortfall.reload()
    log(f"      serving {sc['champion']}: 30-day MAPE {sc['backtest']['model_mape_30d']}% (naive {sc['backtest']['naive_mape_30d']}%)")
    try:  # model monitoring: score these forecasts as the actuals arrive
        from . import monitoring
        log(f"      forecast log: {monitoring.log_forecasts()}")
    except Exception as e:  # noqa: BLE001 — monitoring must never fail the pipeline
        log(f"      forecast log skipped: {e}")
    log("[5/5] Alerts & notifications")
    a = alerts.generate()
    log(f"      {len(a)} alerts ({sum(x['severity'] == 'high' for x in a)} high)")
    log(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    run(simulate="--no-sim" not in sys.argv, refresh_eo="--refresh-eo" in sys.argv)
