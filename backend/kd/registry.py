"""Model registry: versions, metrics, champion, promote / rollback.

Layout: models/registry/<task>/<version>/{artifact.joblib|.pt, card.json}  +  models/registry/index.json
  task      'reserve' (prospectivity) or 'shortfall' (production forecasting)
  version   '<algo>-<YYYYmmdd-HHMMSS>'
Each task has one CHAMPION that serving uses. A new candidate is auto-promoted only if it beats the
champion on the task's primary metric (and is evaluated on the same data fingerprint, or the old
champion's data is stale). Admins can promote any version or roll back to the previous champion.
"""
import json
import shutil
from datetime import datetime

import joblib

from .config import MODELS

ROOT = MODELS / "registry"
INDEX = ROOT / "index.json"
PRIMARY = {  # task → (metric key, higher_is_better)
    "reserve": ("gen_score", True),         # mean of spatial-block CV AUC and held-out-occurrence AUC (>6 km)
    "shortfall": ("mape_30d", False),       # rolling-origin backtest, 30-day totals
}


def _load():
    if INDEX.exists():
        return json.loads(INDEX.read_text())
    return {}


def _save(ix):
    ROOT.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(ix, indent=2, default=str))


def register(task, algo, artifact, card, metrics, data_version, auto_promote=True, torch_state=None):
    """Store a candidate. Returns (version, promoted: bool, reason)."""
    ix = _load()
    t = ix.setdefault(task, dict(champion=None, previous=None, versions=[]))
    version = f"{algo}-{datetime.now():%Y%m%d-%H%M%S%f}"[:-3]
    d = ROOT / task / version
    d.mkdir(parents=True, exist_ok=True)
    if torch_state is not None:
        import torch
        torch.save(torch_state, d / "artifact.pt")
    if artifact is not None:
        joblib.dump(artifact, d / "artifact.joblib")
    (d / "card.json").write_text(json.dumps(card, indent=2, default=str))
    entry = dict(version=version, algo=algo, created=datetime.now().isoformat(timespec="seconds"),
                 metrics=metrics, data_version=data_version, status="candidate")
    t["versions"].append(entry)
    key, higher = PRIMARY[task]
    champ = next((v for v in t["versions"] if v["version"] == t["champion"]), None)
    promoted, reason = False, ""
    if champ is None:
        promoted, reason = True, "first model for this task"
    elif champ["data_version"] != data_version:
        # re-evaluate fairly: only compare against candidates trained on the same data
        same = [v for v in t["versions"] if v["data_version"] == data_version]
        best = (max if higher else min)(same, key=lambda v: v["metrics"].get(key, float("-inf") if higher else float("inf")))
        promoted = best["version"] == version
        reason = "best on current data" if promoted else f"{best['version']} is better on current data"
    else:
        a, b = metrics.get(key), champ["metrics"].get(key)
        better = a is not None and (b is None or (a > b if higher else a < b))
        promoted = better
        reason = f"{key} {a} vs champion {b}"
    if promoted and auto_promote:
        _set_champion(t, version)
    _save(ix)
    return version, promoted and auto_promote, reason


def update_metrics(task, version, metrics):
    ix = _load()
    for v in ix[task]["versions"]:
        if v["version"] == version:
            v["metrics"] = metrics
    _save(ix)


def _set_champion(t, version):
    if t["champion"] and t["champion"] != version:
        t["previous"] = t["champion"]
    t["champion"] = version
    for v in t["versions"]:
        v["status"] = "champion" if v["version"] == version else ("archived" if v["status"] == "champion" else v["status"])


def promote(task, version, manual=False):
    """manual=True (an admin action) also pins the model family: retraining keeps serving that family
    until the pin is cleared, instead of switching to whichever family wins the next leaderboard."""
    ix = _load()
    t = ix[task]
    e = next((v for v in t["versions"] if v["version"] == version), None)
    if e is None:
        raise KeyError(version)
    _set_champion(t, version)
    if manual:
        t["pinned"] = e["algo"]
    _save(ix)


def unpin(task):
    ix = _load()
    ix[task]["pinned"] = None
    _save(ix)


def select_champion(task, candidates, data_version=None):
    """candidates = {algo: dict(version=, metrics=)} from one training run on the current data.
    Pinned family wins if present; otherwise the best primary metric. Returns (algo, reason)."""
    key, higher = PRIMARY[task]
    pin = _load().get(task, {}).get("pinned")
    if pin in candidates:
        algo, why = pin, f"{pin} pinned by an administrator"
    else:
        pick = max if higher else min
        algo = pick(candidates, key=lambda a: candidates[a]["metrics"].get(key, float("-inf") if higher else float("inf")))
        why = f"{algo} has the best {key} ({candidates[algo]['metrics'].get(key)})"
    promote(task, candidates[algo]["version"])
    prune(task)
    return algo, why


def rollback(task):
    ix = _load()
    t = ix[task]
    if not t.get("previous"):
        raise ValueError("no previous champion to roll back to")
    _set_champion(t, t["previous"])
    t["pinned"] = next(v["algo"] for v in t["versions"] if v["version"] == t["champion"])
    _save(ix)
    return t["champion"]


def champion(task):
    """(version entry, directory) of the serving model, or (None, None)."""
    t = _load().get(task)
    if not t or not t["champion"]:
        return None, None
    e = next(v for v in t["versions"] if v["version"] == t["champion"])
    return e, ROOT / task / e["version"]


def load_artifact(task, version=None):
    e, d = champion(task) if version is None else (None, ROOT / task / version)
    return joblib.load(d / "artifact.joblib")


def listing():
    ix = _load()
    return {task: dict(champion=t["champion"], previous=t.get("previous"), pinned=t.get("pinned"),
                       primary_metric=PRIMARY[task][0],
                       higher_is_better=PRIMARY[task][1],
                       versions=sorted(t["versions"], key=lambda v: v["created"], reverse=True)[:40])
            for task, t in ix.items()}


def prune(task, keep=30):
    """Delete artifacts of old archived/candidate versions beyond `keep` (champion + previous always kept)."""
    ix = _load()
    t = ix.get(task)
    if not t:
        return 0
    protect = {t["champion"], t.get("previous")}
    latest = {}
    for v in sorted(t["versions"], key=lambda v: v["created"]):
        latest[v["algo"]] = v["version"]
    protect |= set(latest.values())  # newest per family (e.g. the re-used U-Net and its cached grids)
    old = sorted([v for v in t["versions"] if v["version"] not in protect], key=lambda v: v["created"])[:-keep or None]
    for v in old:
        shutil.rmtree(ROOT / task / v["version"], ignore_errors=True)
    t["versions"] = [v for v in t["versions"] if v not in old]
    _save(ix)
    return len(old)
