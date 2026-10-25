"""Layer 4 · Reserve Identification & Mapping.

1. Labels: known deposits + borehole intercepts (positive), barren holes + distal background
   (negative). Everything else is unlabelled: that's where the model has to find something.
2. Prospectivity classifier: random forest on satellite, terrain, geology, geochemistry and
   geophysics features, including neighbourhood-context features that stand in for a
   CNN/U-Net receptive field. Validated with SPATIAL block cross-validation, because
   random splits leak neighbouring cells and inflate scores.
3. Confidence: stability across five models each trained without one spatial block
   (epistemic spread), discounted where there is no drilling nearby (data support).
4. Probable reserve zones: connected high-prospectivity clusters, ranked.
5. Sub-surface estimation: grade and thickness regressed from borehole intercepts, rolled up
   into an indicative tonnage (UNFC G4 'reconnaissance' level, not a reserve statement).
"""
import json

import joblib
import numpy as np
import pandas as pd
from scipy import ndimage
from scipy.spatial import ConvexHull
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import RidgeCV
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import average_precision_score, r2_score, roc_auc_score
from sklearn.model_selection import GroupKFold, cross_val_predict

from . import geo, storage
from .config import BOUNDS, FEATURE_STORE, KNOWN_DEPOSITS, LAKE, MINES, MODELS, RES, SEED

THRESHOLD = 0.5            # floor for the zone threshold
FALSE_ALARM_BUDGET = 0.03  # at most 3% of background cells (>6 km from any recorded site) may be flagged


def zone_threshold(prob, dep_d):
    """Per-model threshold: models differ in score scale (an ensemble with a CNN scores higher and
    smoother than a random forest), so a fixed cut would give one model tight zones and another
    sprawling ones. Calibrate each to the same false-alarm budget on background cells."""
    return round(max(THRESHOLD, float(np.quantile(prob[dep_d > 6], 1 - FALSE_ALARM_BUDGET))), 3)
DENSITY_T_M3 = 3.8         # manganese ore
DEPTH_EXTENT_M = 100       # assumed down-dip extent per cell
CONTINUITY = 0.2          # fraction of strike length that is ore
CELL_STRIKE_M = 1000
FEATURE_LABELS = {
    "iron_ratio": "Ferric-oxide ratio (B4/B2)", "clay_ratio": "Clay/hydroxyl ratio (B11/B12)", "sar_vh": "SAR VH backscatter",
    "mn_index": "Mn-oxide spectral index", "mn_ctx3_mean": "Mn index (3×3 context)",
    "mn_ctx7_mean": "Mn index (7×7 context)", "mn_ctx7_std": "Mn index texture (7×7)",
    "ndvi": "NDVI", "lst": "Land surface temp", "sar_vv": "SAR backscatter", "elevation": "Elevation",
    "slope": "Slope", "lineament_km": "Distance to lineament", "geochem_log_mn": "Stream-sediment Mn",
    "mag_nT": "Magnetic anomaly", "geo_sausar": "Sausar Group host rock", "geo_gneiss": "Tirodi Gneiss",
    "geo_sakoli": "Sakoli Group", "geo_basalt": "Deccan Trap cover", "geo_alluvium": "Alluvial cover",
}


def feature_label(c):
    """Readable name for a model feature; lithology one-hots (geo_u<code>) come from the legend."""
    if c.startswith("geo_u"):
        try:
            leg = storage.read_table("geology_legend").set_index("code").name
            return f"Lithology: {leg[int(c[5:])]}"
        except Exception:
            return c
    return FEATURE_LABELS.get(c, c)


def _boreholes_real():
    try:
        pv = storage.read_table("provenance")
        return bool(pv[(pv.name == "boreholes") & (pv.real == 1)].shape[0])
    except Exception:
        return False


def _features(df):
    return [c for c in df.columns if c not in ("cell_id", "lat", "lon")]


def load_sites():
    """Recorded manganese sites. REAL when USGS MRDS has been fetched: producers, past producers,
    plants and prospects become 'mine' (training positives); 'Occurrence' records are held out
    to test whether the model finds sites it was never shown. MOIL mines (OSM-verified) are
    always 'mine'. Falls back to the configured deposit list when MRDS is unavailable."""
    rows = [dict(name=m["name"], lat=m["lat"], lon=m["lon"], kind="mine", source=f"MOIL mine ({m['loc_src']})") for m in MINES]
    path = LAKE / "geology" / "usgs_mrds_mn.csv"
    real = path.exists()
    if real:
        df = pd.read_csv(path)
        for r in df[~df.region_record.astype(bool)].itertuples():
            rows.append(dict(name=r.site, lat=r.lat, lon=r.lon, kind="occurrence" if r.status == "Occurrence" else "mine",
                             source=f"USGS MRDS · {r.status}"))
    else:
        rows += [dict(name=n, lat=a, lon=b, kind="mine", source=src) for n, a, b, src in KNOWN_DEPOSITS[len(MINES):]]
    sites = pd.DataFrame(rows)
    m = 0.05
    sites = sites[sites.lat.between(BOUNDS["lat_min"] - m, BOUNDS["lat_max"] + m)
                  & sites.lon.between(BOUNDS["lon_min"] - m, BOUNDS["lon_max"] + m)].reset_index(drop=True)
    return sites, real


def _dist(feats, pts):
    shape = geo.grid()[0].shape
    if not len(pts):
        return np.full(len(feats), np.inf)
    return geo.dist_km_to_points(feats.lat.values.reshape(shape), feats.lon.values.reshape(shape), pts).ravel()


def build_labels(feats, holes, rng, sites, real_sites):
    """Positives: within 1.5 km of a recorded mine. Negatives: random background >6 km from ANY
    recorded site (mines and occurrences). Occurrences themselves stay unlabelled (held out)."""
    shape = geo.grid()[0].shape
    lat, lon = feats.lat.values, feats.lon.values
    mine_d = _dist(feats, list(zip(sites[sites.kind == "mine"].lat, sites[sites.kind == "mine"].lon)))
    occ_d = _dist(feats, list(zip(sites[sites.kind == "occurrence"].lat, sites[sites.kind == "occurrence"].lon)))
    any_d = np.minimum(mine_d, occ_d)
    y = np.full(len(feats), -1)
    y[mine_d <= 1.5] = 1
    if real_sites:
        pool = np.where((y == -1) & (any_d > 6))[0]
        y[rng.choice(pool, size=min(1500, len(pool)), replace=False)] = 0
        return y, mine_d, occ_d
    for h in holes.itertuples():
        rc = geo.cell_of(h.lat, h.lon)
        if not rc:
            continue
        cid = rc[0] * shape[1] + rc[1]
        if h.thickness_m > 0 and h.grade_mn_pct >= 15:
            y[cid] = 1
        elif y[cid] != 1:
            y[cid] = 0
    lat2d, lon2d = lat.reshape(shape), lon.reshape(shape)
    pos_d = geo.dist_km_to_points(lat2d, lon2d, [(lat[i], lon[i]) for i in np.where(y == 1)[0][::3]]).ravel()
    pool = np.where((y == -1) & (pos_d > 6))[0]
    y[rng.choice(pool, size=min(1500, len(pool)), replace=False)] = 0
    return y, mine_d, occ_d


def _feature_layer(c):
    """Which map layer (and so which provenance) a model feature is derived from."""
    if c.startswith("mn_ctx"):
        return "mn_index"
    if c.startswith("geo_"):
        return "geology_unit"
    return {"geochem_log_mn": "geochem_mn_ppm"}.get(c, c)


def train_and_map():
    rng = np.random.default_rng(SEED)
    cat = storage.read_table("layer_catalog", storage.GEODB).set_index("name")

    def real_of(c):
        layer = _feature_layer(c)
        return bool(cat.loc[layer, "real"]) if layer in cat.index and "real" in cat.columns else False

    feats = storage.fs_get("reserve_features")
    holes = storage.read_table("boreholes")
    sites, real_sites = load_sites()
    all_cols = _features(feats)
    real_cols = [c for c in all_cols if real_of(c)]
    # With real labels, train on real inputs only: simulated geology/geochem must not steer a
    # real prospectivity map. (Simulated inputs remain visible as map layers, flagged as such.)
    real_only = real_sites and len(real_cols) >= 5
    cols = real_cols if real_only else all_cols
    y, mine_d, occ_d = build_labels(feats, holes, rng, sites, real_sites)
    dep_d = np.minimum(mine_d, occ_d)
    lab = y >= 0
    X, yl = feats.loc[lab, cols].values, y[lab]

    blocks = (np.floor((feats.lat[lab] - BOUNDS["lat_min"]) / 0.2) * 10
              + np.floor((feats.lon[lab] - BOUNDS["lon_min"]) / 0.25)).values
    rf = RandomForestClassifier(n_estimators=300, min_samples_leaf=2, class_weight="balanced_subsample",
                                max_features="sqrt", n_jobs=-1, random_state=SEED)
    cv = cross_val_predict(rf, X, yl, cv=GroupKFold(5), groups=blocks, method="predict_proba")[:, 1]
    rf.fit(X, yl)
    Xall = feats[cols].values
    shape = geo.grid()[0].shape
    lab_idx = np.where(lab)[0]

    # XGBoost on the same features, labels and spatial folds
    from xgboost import XGBClassifier
    spw = float((yl == 0).sum() / max((yl == 1).sum(), 1))
    xgb = XGBClassifier(n_estimators=400, max_depth=4, learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
                        min_child_weight=2, scale_pos_weight=spw, eval_metric="logloss", n_jobs=-1, random_state=SEED)
    cv_xgb = cross_val_predict(xgb, X, yl, cv=GroupKFold(5), groups=blocks, method="predict_proba")[:, 1]
    xgb.fit(X, yl)

    candidates = {
        "random_forest": dict(algo="Random forest (300 trees) on real raster features", prob=rf.predict_proba(Xall)[:, 1], cv=cv),
        "xgboost": dict(algo="XGBoost (400 trees, depth 4) on real raster features", prob=xgb.predict_proba(Xall)[:, 1], cv=cv_xgb),
    }
    un = _unet_candidate(sites, real_sites, lab_idx)
    if un:
        candidates["unet"] = un
    comps = list(candidates)
    candidates["ensemble"] = dict(algo=f"Mean of {', '.join(comps)}",
                                  prob=np.mean([candidates[c]["prob"] for c in comps], axis=0),
                                  cv=np.mean([candidates[c]["cv"] for c in comps], axis=0))

    from . import registry
    data_version = _data_version(sites)
    board = []
    for name, c in candidates.items():
        c["thr"] = zone_threshold(c["prob"], dep_d)
        oc = occurrence_check(c["prob"], sites, dep_d, shape, np.random.default_rng(SEED), c["thr"]) if real_sites else {}
        cv_auc = round(float(roc_auc_score(yl, c["cv"])), 3)
        mt = dict(cv_auc=cv_auc, pr_auc=round(float(average_precision_score(yl, c["cv"])), 3),
                  occ_auc=oc.get("auc"), occ_auc_far=oc.get("auc_far"), hit_rate=oc.get("hit_rate"),
                  background_flag_rate=oc.get("background_flag_rate"),
                  # two independent held-out tests: unseen regions (spatial CV) and unseen deposits (far occurrences)
                  gen_score=round((cv_auc + oc["auc_far"]) / 2, 3) if oc.get("auc_far") is not None else cv_auc)
        art = {"random_forest": dict(model=rf, cols=cols), "xgboost": dict(model=xgb, cols=cols)}.get(name)
        if name == "ensemble":
            art = dict(components=comps)
        if c.get("reused"):  # same imagery + labels: keep the registered U-Net, don't duplicate it
            version, promoted, why = c["reused"], False, "re-used (inputs unchanged)"
        else:
            version, promoted, why = registry.register("reserve", name, art if name != "unet" else dict(bands=c.get("bands")),
                                                       dict(algorithm=c["algo"], metrics=mt, occurrence_check=oc), mt, data_version,
                                                       torch_state=c.get("torch_state"), auto_promote=False)
            if "_grids" in c:
                np.savez_compressed(registry.ROOT / "reserve" / version / "grids.npz", prob=c["_grids"][0], oof=c["_grids"][1])
        registry.update_metrics("reserve", version, mt)
        c.update(version=version, metrics=mt, occ=oc)
        board.append(dict(name=name, algorithm=c["algo"], version=version, **mt))
    # Champion: an admin-pinned family if any, else the best generalisation score of this run.
    champ_name, why = registry.select_champion("reserve", candidates, data_version)
    for b in board:
        b["champion"] = b["name"] == champ_name
    print(f"      reserve champion: {champ_name} | {why}")
    prob = candidates[champ_name]["prob"]
    cv = candidates[champ_name]["cv"]
    fold_preds = []
    for tr, _ in GroupKFold(5).split(X, yl, blocks):
        m = RandomForestClassifier(n_estimators=100, min_samples_leaf=2, class_weight="balanced_subsample",
                                   n_jobs=-1, random_state=SEED).fit(X[tr], yl[tr])
        fold_preds.append(m.predict_proba(Xall)[:, 1])
    spread = np.std(fold_preds, axis=0)
    # data support: near recorded sites (real) or drill holes (simulated mode) the model is better constrained
    support_d = dep_d if real_sites else geo.dist_km_to_points(feats.lat.values.reshape(shape), feats.lon.values.reshape(shape),
                                                               list(zip(holes.lat, holes.lon))).ravel()
    support = np.exp(-support_d / 4)
    conf = np.clip(1 - spread / 0.25, 0, 1) * (0.6 + 0.4 * support)

    # Sub-surface: grade & thickness from mineralised intercepts.
    hits = holes[(holes.thickness_m > 0)].copy()
    cid = [(lambda rc: rc[0] * geo.grid()[0].shape[1] + rc[1])(geo.cell_of(a, b)) for a, b in zip(hits.lat, hits.lon)]
    Xh = feats.loc[cid, cols].values
    sub = {}
    for target in ("grade_mn_pct", "thickness_m"):
        g = make_pipeline(StandardScaler(), RidgeCV(alphas=np.logspace(-2, 3, 20)))
        pred = cross_val_predict(g, Xh, hits[target].values, cv=5)
        g.fit(Xh, hits[target].values)
        sub[target] = dict(model=g, r2=float(r2_score(hits[target].values, pred)),
                           mae=float(np.mean(np.abs(hits[target].values - pred))))
    grade = np.clip(sub["grade_mn_pct"]["model"].predict(Xall), 10, 48)
    thick = np.clip(sub["thickness_m"]["model"].predict(Xall), 0.5, 16)
    thr = candidates[champ_name]["thr"]
    tonnes = CELL_STRIKE_M * DEPTH_EXTENT_M * CONTINUITY * thick * DENSITY_T_M3 * prob * (prob >= thr)

    zones = _zones(prob, conf, grade, thick, tonnes, mine_d, occ_d, sites, thr)

    occ_check = candidates[champ_name]["occ"]

    # Synthetic sanity check: does the model light up the unmined horizons we planted?
    # Only meaningful while the imagery itself is simulated from that truth; with real
    # Sentinel-2 the planted horizons are not what the satellite sees, so the check is skipped.
    truth_p = LAKE.parent / "_synthetic_truth_F.npy"
    s2_prov = LAKE / "satellite" / "sentinel2_ndvi.provenance.json"
    hidden = {}
    if truth_p.exists() and not s2_prov.exists() and not real_sites:
        F = np.load(truth_p).ravel()
        far = dep_d > 6
        hidden = dict(auc_far_from_known=float(roc_auc_score(F[far] > 0.35, prob[far])),
                      note="Cells >6 km from any known deposit vs planted ground truth (simulation only).")

    MODELS.mkdir(exist_ok=True)
    joblib.dump(dict(rf=rf, cols=cols, grade=sub["grade_mn_pct"]["model"], thick=sub["thickness_m"]["model"], champion=champ_name),
                MODELS / "reserve.joblib")
    card = dict(
        name="Prospectivity classifier", algorithm=candidates[champ_name]["algo"], champion=champ_name,
        champion_version=candidates[champ_name]["version"], leaderboard=board,
        explanation_model="random forest (local explanations and feature importance come from the RF, for every champion)",
        inputs="real only" if real_only else "real + simulated",
        label_source="USGS MRDS producers/past producers + OSM-verified MOIL mines" if real_sites else "configured deposit list (simulated mode)",
        sites=dict(mines=int((sites.kind == "mine").sum()), occurrences=int((sites.kind == "occurrence").sum())),
        occurrence_check=occ_check,
        subsurface_data=None if _boreholes_real() else "SIMULATED boreholes: no public borehole logs exist for the belt; upload real logs to replace",
        subsurface_algorithm="Ridge regression on borehole intercepts (grade, thickness)",
        labels=dict(positive=int((y == 1).sum()), negative=int((y == 0).sum()), unlabelled=int((y == -1).sum())),
        validation="5-fold spatial block CV (0.2° × 0.25° blocks)",
        roc_auc=round(float(roc_auc_score(yl, cv)), 3), pr_auc=round(float(average_precision_score(yl, cv)), 3),
        importance=sorted([dict(feature=feature_label(c), key=c, value=round(float(v), 4), real=real_of(c))
                           for c, v in zip(cols, rf.feature_importances_)], key=lambda d: -d["value"]),
        real_share=round(float(sum(v for c, v in zip(cols, rf.feature_importances_) if real_of(c))), 3),
        subsurface={k: dict(r2=round(v["r2"], 3), mae=round(v["mae"], 2), n=len(hits)) for k, v in sub.items()},
        hidden_horizon_check=hidden, threshold=thr,
        threshold_rule=f"per model: the score flagging {FALSE_ALARM_BUDGET:.0%} of background cells (>6 km from any recorded site), floor {THRESHOLD}",
        tonnage_assumptions=dict(density_t_m3=DENSITY_T_M3, depth_extent_m=DEPTH_EXTENT_M,
                                 continuity=CONTINUITY, cell_strike_m=CELL_STRIKE_M),
    )
    (MODELS / "reserve_card.json").write_text(json.dumps(card, indent=2))

    storage.add_grid_columns(
        dict(prospectivity=prob, confidence=conf, est_grade=grade, est_thickness=thick, est_tonnes=tonnes,
             label=y.astype(float)),
        {"prospectivity": dict(unit="p", group="AI output", desc=f"Mn prospectivity ({champ_name.replace('_', ' ')})"),
         "confidence": dict(unit="", group="AI output", desc="Model confidence (tree agreement)"),
         "est_grade": dict(unit="% Mn", group="AI output", desc="Estimated ore grade"),
         "est_thickness": dict(unit="m", group="AI output", desc="Estimated ore thickness"),
         "est_tonnes": dict(unit="t", group="AI output", desc="Indicative tonnage per cell"),
         "label": dict(unit="class", group="AI output", desc="Training label (1/0/-1)")})
    storage.write_table("reserve_zones", pd.DataFrame([{k: v for k, v in z.items() if k != "polygon"} for z in zones]))
    storage.write_vector("reserve_zones", {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {k: v for k, v in z.items() if k != "polygon"},
         "geometry": {"type": "Polygon", "coordinates": [z["polygon"]]}} for z in zones]})
    storage.write_vector("mines", {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {k: m[k] for k in ("id", "name", "district", "state", "method", "capacity_tpd", "loc_src")},
         "geometry": {"type": "Point", "coordinates": [m["lon"], m["lat"]]}} for m in MINES]})
    storage.write_vector("known_deposits", {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": r.name, "kind": r.kind, "source": r.source},
         "geometry": {"type": "Point", "coordinates": [r.lon, r.lat]}} for r in sites.itertuples()]})
    storage.write_vector("boreholes", {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {k: (None if pd.isna(v) else v) for k, v in h.items() if k not in ("lat", "lon")},
         "geometry": {"type": "Point", "coordinates": [h["lon"], h["lat"]]}} for h in holes.to_dict("records")]})
    storage.log_run(4, "reserve model", roc_auc=card["roc_auc"], zones=len(zones),
                    new_zones=sum(z["status"] == "new" for z in zones))
    return card, zones


MAX_ZONE_CELLS = 60  # ≈ 70 km²: larger connected areas are split into their high-probability cores


def _zone_labels(p2d, thr):
    """Hierarchical thresholding. Components above `thr` that are larger than MAX_ZONE_CELLS (smooth
    models light up whole mining districts) are re-cut at thr + 0.1, + 0.2 … until each piece is
    target-sized; cells that never form a compact core are left out of the zones."""
    out = np.zeros(p2d.shape, dtype=int)
    n = 0
    stack = [(np.ones(p2d.shape, bool), thr)]
    while stack:
        region, t = stack.pop()
        lab, k = ndimage.label(region & (p2d >= t), structure=np.ones((3, 3)))
        for i in range(1, k + 1):
            comp = lab == i
            if comp.sum() > MAX_ZONE_CELLS and t + 0.1 < 0.99:
                stack.append((comp, round(t + 0.1, 2)))
            else:
                n += 1
                out[comp] = n
    return out, n


def _zones(prob, conf, grade, thick, tonnes, mine_d, occ_d, sites, thr=THRESHOLD):
    shape = geo.grid()[0].shape
    lat2d, lon2d = geo.grid()
    lab, n = _zone_labels(prob.reshape(shape), thr)
    zones = []
    h = RES / 2
    for k in range(1, n + 1):
        idx = np.flatnonzero(lab.ravel() == k)
        if len(idx) < 4:
            continue
        la, lo = lat2d.ravel()[idx], lon2d.ravel()[idx]
        corners = np.array([(x + dx, y + dy) for x, y in zip(lo, la) for dx in (-h, h) for dy in (-h, h)])
        hull = corners[ConvexHull(corners).vertices]
        poly = [[round(float(x), 4), round(float(y), 4)] for x, y in hull] + [[round(float(hull[0][0]), 4), round(float(hull[0][1]), 4)]]
        status = "mine" if float(mine_d[idx].min()) < 3.0 else ("occurrence" if float(occ_d[idx].min()) < 3.0 else "new")
        near = sites.iloc[int(((sites.lat - la.mean()) ** 2 + (sites.lon - lo.mean()) ** 2).values.argmin())]
        zones.append(dict(
            cells=len(idx), area_km2=round(len(idx) * 1.144, 1),
            centroid_lat=round(float(la.mean()), 4), centroid_lon=round(float(lo.mean()), 4),
            mean_prob=round(float(prob[idx].mean()), 3), max_prob=round(float(prob[idx].max()), 3),
            confidence=round(float(conf[idx].mean()), 3), est_grade_pct=round(float(grade[idx].mean()), 1),
            est_thickness_m=round(float(thick[idx].mean()), 1), est_tonnes_mt=round(float(tonnes[idx].sum() / 1e6), 2),
            status=status, nearest_known=near["name"], nearest_source=near["source"],
            unfc={"mine": "G2/G3 (explored)", "occurrence": "G4 (recorded occurrence)", "new": "G4 (reconnaissance)"}[status],
            polygon=poly,
        ))
    order = {"new": 0, "occurrence": 1, "mine": 2}
    zones.sort(key=lambda z: (order[z["status"]], -z["est_tonnes_mt"] * z["confidence"]))
    for i, z in enumerate(zones):
        z["zone_id"] = f"{dict(new='NZ', occurrence='OZ', mine='KZ')[z['status']]}-{i+1:02d}"
        z["priority"] = {"mine": "Brownfield", "occurrence": "Confirmed"}.get(
            z["status"], "High" if z["mean_prob"] >= 0.62 and z["cells"] >= 6 else "Medium")
    return zones


def _data_version(sites):
    import hashlib
    meta = json.loads((FEATURE_STORE / "reserve_features.meta.json").read_text())["version"]
    stack = LAKE / "satellite" / "unet_stack.provenance.json"
    st = json.loads(stack.read_text()).get("fetched_at", "") if stack.exists() else ""
    sh = hashlib.sha1(sites[["lat", "lon", "kind"]].to_csv(index=False).encode()).hexdigest()[:8]
    return f"{meta}-{sh}-{st[:10]}"


def _unet_candidate(sites, real_sites, lab_idx):
    """U-Net probabilities: re-use the registered model for this exact imagery + label set, or train
    (5-fold spatial CV + final fit, a few minutes on CPU). Set KD_TRAIN_DL=1 to force retraining."""
    import os
    from . import registry, unet
    if not (LAKE / "satellite" / "unet_stack.npz").exists() or not real_sites:
        return None
    try:
        import torch  # noqa: F401
    except ImportError:
        return None
    dv = _data_version(sites)
    ix = registry.listing().get("reserve", {})
    prior = next((v for v in ix.get("versions", []) if v["algo"] == "unet" and v["data_version"] == dv), None)
    shape = geo.grid()[0].shape
    if prior and os.environ.get("KD_TRAIN_DL") != "1":
        import torch
        cache = registry.ROOT / "reserve" / prior["version"]
        z = np.load(cache / "grids.npz")
        return dict(algo="U-Net (16-32-64-128) on real Sentinel-2 + DEM at 0.002°, pooled to 0.01°",
                    prob=z["prob"].ravel(), cv=z["oof"].ravel()[lab_idx], bands=None, reused=prior["version"])
    res = unet.train_with_cv(sites, steps=250, log=print)
    oof = res["oof_grid"].ravel()
    out = dict(algo="U-Net (16-32-64-128) on real Sentinel-2 + DEM at 0.002°, pooled to 0.01°",
               prob=res["prob_grid"].ravel(), cv=oof[lab_idx], bands=res["bands"], torch_state=res["model"].state_dict())
    out["_grids"] = (res["prob_grid"], res["oof_grid"])
    return out


def occurrence_check(prob, sites, dep_d, shape, rng, thr=THRESHOLD):
    """Held-out USGS occurrences vs background (>6 km from any site), overall and by distance to the
    nearest TRAINING mine. auc_far = occurrences >6 km from any training mine (the generalisation test)."""
    occ = sites[sites.kind == "occurrence"]
    if not len(occ):
        return {}
    bg = rng.choice(np.where(dep_d > 6)[0], size=min(3000, int((dep_d > 6).sum())), replace=False)
    neigh = ndimage.maximum_filter(prob.reshape(shape), size=3).ravel()  # MRDS positions are rounded
    mines = sites[sites.kind == "mine"]
    rows = []
    for r in occ.itertuples():
        rc = geo.cell_of(r.lat, r.lon)
        if rc:
            rows.append((float(np.min(np.hypot((mines.lat - r.lat) * 110.57, (mines.lon - r.lon) * 103.4))),
                         float(neigh[rc[0] * shape[1] + rc[1]])))
    rows = np.array(rows)
    auc = lambda m: round(float(roc_auc_score(np.r_[np.ones(m.sum()), np.zeros(len(bg))], np.r_[rows[m, 1], neigh[bg]])), 3)
    bands = []
    for lo_, hi_ in ((0, 3), (3, 6), (6, 10), (10, 999)):
        m = (rows[:, 0] >= lo_) & (rows[:, 0] < hi_)
        if m.sum():
            bands.append(dict(band=f"{lo_}–{hi_} km" if hi_ < 999 else f">{lo_} km", n=int(m.sum()), auc=auc(m),
                              hit_rate=round(float((rows[m, 1] >= thr).mean()), 3)))
    far = rows[:, 0] > 6
    return dict(n_occurrences=len(rows), auc=auc(np.ones(len(rows), bool)), auc_far=auc(far) if far.sum() else None,
                n_far=int(far.sum()), hit_rate=round(float((rows[:, 1] >= thr).mean()), 3), threshold=thr,
                background_flag_rate=round(float((neigh[bg] >= thr).mean()), 3), by_distance=bands,
                note="USGS MRDS 'Occurrence' sites were held out of training. AUC compares the model's score at those sites "
                     "(3×3 max, MRDS positions are rounded) with background cells >6 km from any recorded site.")


_cache = {}


def explain_cell(cell_id):
    """Local explanation: how much the probability moves when each feature is set to the
    AOI median (one at a time). Positive = the feature pushes this cell towards 'ore'."""
    if "m" not in _cache:
        _cache["m"] = joblib.load(MODELS / "reserve.joblib")
        _cache["f"] = storage.fs_get("reserve_features")
    m, feats = _cache["m"], _cache["f"]
    cols = m["cols"]
    x = feats.loc[[cell_id], cols]
    base = m["rf"].predict_proba(x.values)[0, 1]
    med = feats[cols].median()
    rows = []
    for c in cols:
        xx = x.copy(); xx[c] = med[c]
        rows.append(dict(feature=feature_label(c), key=c, value=float(x[c].iloc[0]),
                         contribution=round(float(base - m["rf"].predict_proba(xx.values)[0, 1]), 4)))
    rows.sort(key=lambda r: -abs(r["contribution"]))
    return dict(prob=float(base), contributions=rows)
