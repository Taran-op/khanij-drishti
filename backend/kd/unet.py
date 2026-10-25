"""Layer 4 · U-Net prospectivity from real Sentinel-2 imagery (the diagram's "CNN / U-Net").

Input   real dry-season Sentinel-2 median (7 bands + NDVI) + Copernicus DEM (elevation, slope),
        0.002° ≈ 220 m, 400 × 750 px (kd/eo.py unet_stack)
Labels  weak, pixel-level: 1 within 1.5 km of a recorded mine (USGS MRDS producer / past producer /
        MOIL); 0 more than 6 km from ANY recorded site; everything else ignored (masked loss).
        USGS 'Occurrence' sites are never labels: they are the held-out test.
Model   compact U-Net (16-32-64-128 channels), BCE with class weighting, flips/rot90 augmentation,
        random 128 px crops. CPU-friendly (~1 min per fit on a desktop).
Output  per-pixel probability, average-pooled 5×5 onto the 0.01° analysis grid.
Validation  5-fold spatial-block CV on the same 0.2° × 0.25° blocks as the random forest: labels
        inside held-out blocks are hidden from training and predicted afterwards.
"""
import json

import numpy as np

from . import geo
from .config import BOUNDS, LAKE, RES, SEED

FINE = 5  # fine pixels per grid cell side


def _torch():
    import torch
    import torch.nn as nn
    return torch, nn


def load_stack():
    z = np.load(LAKE / "satellite" / "unet_stack.npz")
    x = z["data"].astype("float32")
    names = json.loads(str(z["meta"]))["bands"]
    for c in range(x.shape[0]):  # fill gaps with the channel median, then standardise
        med = np.nanmedian(x[c])
        x[c] = np.where(np.isfinite(x[c]), x[c], med)
        x[c] = (x[c] - x[c].mean()) / (x[c].std() + 1e-6)
    return x, names


def fine_coords(H, W):
    lat = BOUNDS["lat_max"] - (np.arange(H) + 0.5) * (RES / FINE)
    lon = BOUNDS["lon_min"] + (np.arange(W) + 0.5) * (RES / FINE)
    lon2d, lat2d = np.meshgrid(lon, lat)
    return lat2d, lon2d


def fine_targets(sites, H, W):
    lat2d, lon2d = fine_coords(H, W)
    mines = sites[sites.kind == "mine"]
    mine_d = geo.dist_km_to_points(lat2d, lon2d, list(zip(mines.lat, mines.lon)))
    any_d = geo.dist_km_to_points(lat2d, lon2d, list(zip(sites.lat, sites.lon)))
    y = np.full((H, W), -1.0, dtype="float32")
    y[any_d > 6] = 0
    y[mine_d <= 1.5] = 1
    blocks = (np.floor((lat2d - BOUNDS["lat_min"]) / 0.2) * 10 + np.floor((lon2d - BOUNDS["lon_min"]) / 0.25)).astype(int)
    return y, blocks


def make_model():
    torch, nn = _torch()

    def block(i, o):
        return nn.Sequential(nn.Conv2d(i, o, 3, padding=1), nn.BatchNorm2d(o), nn.ReLU(inplace=True),
                             nn.Conv2d(o, o, 3, padding=1), nn.BatchNorm2d(o), nn.ReLU(inplace=True))

    class UNet(nn.Module):
        def __init__(self, cin, base=16):
            super().__init__()
            self.e1, self.e2, self.e3 = block(cin, base), block(base, base * 2), block(base * 2, base * 4)
            self.b = block(base * 4, base * 8)
            self.pool = nn.MaxPool2d(2)
            self.u3, self.d3 = nn.ConvTranspose2d(base * 8, base * 4, 2, 2), block(base * 8, base * 4)
            self.u2, self.d2 = nn.ConvTranspose2d(base * 4, base * 2, 2, 2), block(base * 4, base * 2)
            self.u1, self.d1 = nn.ConvTranspose2d(base * 2, base, 2, 2), block(base * 2, base)
            self.out = nn.Conv2d(base, 1, 1)
            self.drop = nn.Dropout2d(0.1)

        def forward(self, x):
            e1 = self.e1(x); e2 = self.e2(self.pool(e1)); e3 = self.e3(self.pool(e2))
            b = self.drop(self.b(self.pool(e3)))
            d3 = self.d3(torch.cat([self.u3(b), e3], 1))
            d2 = self.d2(torch.cat([self.u2(d3), e2], 1))
            d1 = self.d1(torch.cat([self.u1(d2), e1], 1))
            return self.out(d1)

    return UNet


def _pad8(x):
    H, W = x.shape[-2:]
    ph, pw = (-H) % 8, (-W) % 8
    return np.pad(x, [(0, 0)] * (x.ndim - 2) + [(0, ph), (0, pw)], mode="reflect"), (H, W)


def fit(x, y, steps=450, crop=128, batch=12, seed=SEED, log=print):
    torch, nn = _torch()
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    UNet = make_model()
    model = UNet(x.shape[0])
    opt = torch.optim.AdamW(model.parameters(), lr=2e-3, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=3e-3, total_steps=steps)
    pos = (y == 1).sum()
    neg = (y == 0).sum()
    pos_w = torch.tensor(float(neg / max(pos, 1)) ** 0.5)
    H, W = y.shape
    # sample crops centred on labelled pixels (half on positives) so every batch carries signal
    pos_idx, neg_idx = np.argwhere(y == 1), np.argwhere(y == 0)
    model.train()
    for step in range(steps):
        xb, yb = [], []
        for k in range(batch):
            src = pos_idx if (k % 2 == 0 and len(pos_idx)) else neg_idx
            cy, cx = src[rng.integers(len(src))]
            r0 = int(np.clip(cy - crop // 2 + rng.integers(-24, 25), 0, H - crop))
            c0 = int(np.clip(cx - crop // 2 + rng.integers(-24, 25), 0, W - crop))
            xi, yi = x[:, r0:r0 + crop, c0:c0 + crop], y[r0:r0 + crop, c0:c0 + crop]
            kk = rng.integers(4)
            xi, yi = np.rot90(xi, kk, (1, 2)), np.rot90(yi, kk)
            if rng.random() < 0.5:
                xi, yi = xi[:, :, ::-1], yi[:, ::-1]
            xb.append(xi.copy()); yb.append(yi.copy())
        xb, yb = torch.from_numpy(np.stack(xb)), torch.from_numpy(np.stack(yb))[:, None]
        logits = model(xb)
        m = (yb >= 0).float()
        loss = (nn.functional.binary_cross_entropy_with_logits(logits, yb.clamp(min=0), pos_weight=pos_w, reduction="none") * m).sum() / m.sum().clamp(min=1)
        opt.zero_grad(); loss.backward(); opt.step(); sched.step()
        if step % 150 == 0 or step == steps - 1:
            log(f"      U-Net step {step:>3}/{steps} loss {loss.item():.3f}")
    return model


def predict(model, x):
    torch, _ = _torch()
    model.eval()
    xp, (H, W) = _pad8(x)
    with torch.no_grad():
        p = torch.sigmoid(model(torch.from_numpy(xp[None]))).numpy()[0, 0, :H, :W]
    return p


def pool_to_grid(p):
    H, W = p.shape
    return p.reshape(H // FINE, FINE, W // FINE, FINE).mean(axis=(1, 3))


def train_with_cv(sites, steps=450, folds=5, log=print):
    """Spatial-block CV then a final fit on all labels. Returns dict with grid probs (final + out-of-fold)."""
    from sklearn.model_selection import GroupKFold
    torch, _ = _torch()
    torch.set_num_threads(max(1, torch.get_num_threads()))
    x, names = load_stack()
    y, blocks = fine_targets(sites, *x.shape[1:])
    lab_blocks = np.unique(blocks[y >= 0])
    oof = np.full(y.shape, np.nan, dtype="float32")
    for k, (_, test_b) in enumerate(GroupKFold(folds).split(lab_blocks, groups=lab_blocks)):
        held = np.isin(blocks, lab_blocks[test_b])
        y_tr = np.where(held, -1, y)
        if (y_tr == 1).sum() == 0:
            continue
        log(f"    U-Net fold {k + 1}/{folds}: {int((y[held] == 1).sum())} positive px held out")
        m = fit(x, y_tr, steps=steps, seed=SEED + k, log=lambda *_: None)
        p = predict(m, x)
        oof[held] = p[held]
    log("    U-Net final fit on all labels")
    final = fit(x, y, steps=steps, log=log)
    p = predict(final, x)
    return dict(model=final, prob_grid=pool_to_grid(p), oof_grid=pool_to_grid(np.nan_to_num(oof, nan=np.nanmean(oof))),
                oof_valid_grid=pool_to_grid(np.isfinite(oof).astype("float32")) > 0.5, bands=names, steps=steps,
                n_pos_px=int((y == 1).sum()), n_neg_px=int((y == 0).sum()))
