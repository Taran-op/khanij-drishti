"""Layer 4 · Production forecaster families (the diagram's "LSTM / Prophet").

All three families share one contract so they can be back-tested on the same rolling origins and
served interchangeably from the model registry:

    fit(df)                       df = integrated daily table (production_daily) up to the origin
    predict(model, rows, hist)    rows = future scenario rows from shortfall.build_scenario
                                  hist = daily table up to the scenario start (context for sequence models)
                                  -> (p10, p50, p90) in tonnes/day

gbm      histogram gradient boosting on the day's drivers, quantile heads (the original model)
lstm     sequence-to-sequence LSTM (PyTorch): an encoder reads the last 28 days of output + drivers,
         a decoder reads the scenario's future drivers and emits P10/P50/P90 per day (pinball loss)
prophet  Meta Prophet per mine: trend + yearly/weekly seasonality + driver regressors; P10/P90
         from the mine's in-sample residual quantiles (uncertainty sampling off, so the optimiser
         can call it hundreds of times)
"""
import logging

import numpy as np
import pandas as pd

from .config import MINES, SEED
from .processing import PRODUCTION_FEATURES, production_features

FAMILIES = ("gbm", "lstm", "prophet")
ALGO = dict(
    gbm="Histogram gradient boosting (driver model) · P10/P50/P90 quantile heads",
    lstm="Seq2seq LSTM (PyTorch, 48 units): 28-day encoder + driver decoder · pinball loss P10/P50/P90",
    prophet="Prophet per mine: trend + yearly/weekly seasonality + 7 driver regressors · residual-quantile P10/P90",
)


def _util(df):
    return (df.actual_t / df.capacity_tpd).values


# ---- GBM -------------------------------------------------------------------------------------
def _gbm_model(loss="squared_error", q=None):
    from sklearn.ensemble import HistGradientBoostingRegressor
    kw = dict(loss=loss, max_iter=300, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=20,
              categorical_features=[PRODUCTION_FEATURES.index("mine_code")], random_state=SEED)
    if q is not None:
        kw["quantile"] = q
    return HistGradientBoostingRegressor(**kw)


def gbm_fit(df):
    X, y = production_features(df), _util(df)
    return dict(p50=_gbm_model().fit(X, y), p10=_gbm_model("quantile", 0.1).fit(X, y),
                p90=_gbm_model("quantile", 0.9).fit(X, y))


def gbm_predict(models, rows, hist=None):
    X = production_features(rows)
    return tuple(np.clip(models[k].predict(X), 0, None) for k in ("p10", "p50", "p90"))


# ---- LSTM ------------------------------------------------------------------------------------
ENC, DEC = 28, 30
NUM = [c for c in PRODUCTION_FEATURES if c != "mine_code"]
MINE_IDS = [m["id"] for m in MINES]
QS = (0.1, 0.5, 0.9)


def _lstm_class():
    import torch
    import torch.nn as nn

    class Seq2Seq(nn.Module):
        def __init__(self, f, hidden=48):
            super().__init__()
            self.enc = nn.LSTM(f + 1, hidden, batch_first=True)
            self.dec = nn.LSTM(f, hidden, batch_first=True)
            self.head = nn.Sequential(nn.Linear(hidden, 32), nn.ReLU(), nn.Linear(32, 3))

        def forward(self, past, future):
            _, state = self.enc(past)
            out, _ = self.dec(future, state)
            z = self.head(out)
            mid = z[..., 1]
            sp = nn.functional.softplus
            return torch.stack([mid - sp(z[..., 0]), mid, mid + sp(z[..., 2])], -1)  # quantiles never cross

    return Seq2Seq


def _lstm_matrix(df, scaler):
    X = production_features(df)
    num = ((X[NUM] - scaler["mu"]) / scaler["sd"]).fillna(0).values.astype("float32")
    hot = np.stack([(df.mine_id == m).values for m in MINE_IDS], 1).astype("float32")
    return np.concatenate([num, hot], 1)


def lstm_fit(df, epochs=12, stride=2, log=lambda *_: None):
    import torch
    torch.manual_seed(SEED)
    rng = np.random.default_rng(SEED)
    X = production_features(df)
    scaler = dict(mu=X[NUM].mean(), sd=X[NUM].std().replace(0, 1))
    past, fut, tgt = [], [], []
    for mid in MINE_IDS:
        d = df[df.mine_id == mid].sort_values("date")
        if len(d) < ENC + DEC:
            continue
        F, u = _lstm_matrix(d, scaler), _util(d).astype("float32")
        u = np.nan_to_num(u, nan=float(np.nanmean(u)))
        for i in range(ENC, len(d) - DEC + 1, stride):
            past.append(np.concatenate([F[i - ENC:i], u[i - ENC:i, None]], 1))
            fut.append(F[i:i + DEC])
            tgt.append(u[i:i + DEC])
    past, fut, tgt = map(lambda a: torch.from_numpy(np.stack(a)), (past, fut, tgt))
    model = _lstm_class()(fut.shape[-1])
    opt = torch.optim.AdamW(model.parameters(), lr=3e-3, weight_decay=1e-4)
    n, bs = len(tgt), 64
    steps = epochs * ((n + bs - 1) // bs)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=4e-3, total_steps=steps)
    q = torch.tensor(QS)
    model.train()
    for ep in range(epochs):
        perm = rng.permutation(n)
        tot = 0.0
        for b in range(0, n, bs):
            idx = torch.from_numpy(perm[b:b + bs])
            pred = model(past[idx], fut[idx])
            err = tgt[idx][..., None] - pred
            loss = torch.maximum(q * err, (q - 1) * err).mean()
            opt.zero_grad(); loss.backward(); opt.step(); sched.step()
            tot += loss.item() * len(idx)
        log(f"      LSTM epoch {ep + 1}/{epochs} pinball {tot / n:.4f}")
    return dict(state={k: v.detach().clone() for k, v in model.state_dict().items()}, scaler=scaler,
                features=int(fut.shape[-1]), samples=n)


def _lstm_net(art):
    if "_net" not in art:
        net = _lstm_class()(art["features"])
        net.load_state_dict(art["state"])
        net.eval()
        art["_net"] = net
    return art["_net"]


def lstm_predict(art, rows, hist):
    import torch
    net = _lstm_net(art)
    out = [np.zeros(len(rows)) for _ in range(3)]
    for mid in rows.mine_id.unique():
        sel = (rows.mine_id == mid).values
        r = rows[sel]
        h = hist[(hist.mine_id == mid) & (hist.date < r.date.min())].sort_values("date").tail(ENC)
        u = np.nan_to_num(_util(h).astype("float32"), nan=float(np.nanmean(_util(h))))
        p = np.concatenate([_lstm_matrix(h, art["scaler"]), u[:, None]], 1)
        with torch.no_grad():
            pr = net(torch.from_numpy(p[None]), torch.from_numpy(_lstm_matrix(r, art["scaler"])[None]))[0].numpy()
        for k in range(3):
            out[k][sel] = np.clip(pr[:, k], 0, None)
    return tuple(out)


# ---- Prophet ---------------------------------------------------------------------------------
REGRESSORS = ["rain_mm", "api", "excavator_avail", "fleet_ratio", "manpower_ratio", "blasts", "festival"]


def _quiet():
    import cmdstanpy  # noqa: F401  (attaches its own handler on import; silence it afterwards)
    for name in ("prophet", "cmdstanpy"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.ERROR)
        lg.disabled = True


def prophet_fit(df):
    from prophet import Prophet
    _quiet()
    out = {}
    for mid in MINE_IDS:
        d = df[df.mine_id == mid].sort_values("date")
        if len(d) < 60:
            continue
        frame = pd.DataFrame(dict(ds=pd.to_datetime(d.date), y=_util(d)))
        for c in REGRESSORS:
            frame[c] = d[c].fillna(d[c].median()).values
        m = Prophet(yearly_seasonality=True, weekly_seasonality=True, daily_seasonality=False,
                    changepoint_prior_scale=0.05, uncertainty_samples=0)
        for c in REGRESSORS:
            m.add_regressor(c, standardize=True)
        m.fit(frame.dropna(subset=["y"]))
        res = frame.y.values - m.predict(frame).yhat.values
        res = res[np.isfinite(res)]
        out[mid] = dict(model=m, q10=float(np.quantile(res, 0.1)), q90=float(np.quantile(res, 0.9)),
                        medians={c: float(d[c].median()) for c in REGRESSORS})
    return out


def prophet_predict(models, rows, hist=None):
    _quiet()
    out = [np.zeros(len(rows)) for _ in range(3)]
    for mid in rows.mine_id.unique():
        sel = (rows.mine_id == mid).values
        r, m = rows[sel], models[mid]
        frame = pd.DataFrame(dict(ds=pd.to_datetime(r.date)))
        for c in REGRESSORS:
            frame[c] = r[c].fillna(m["medians"][c]).values
        yhat = m["model"].predict(frame).yhat.values
        for k, v in enumerate((yhat + m["q10"], yhat, yhat + m["q90"])):
            out[k][sel] = np.clip(v, 0, None)
    return tuple(out)


# ---- dispatch --------------------------------------------------------------------------------
def fit(family, df, log=lambda *_: None):
    if family == "gbm":
        return gbm_fit(df)
    if family == "lstm":
        return lstm_fit(df, log=log)
    if family == "prophet":
        return prophet_fit(df)
    raise KeyError(family)


def predict(family, model, rows, hist):
    """-> (p10, p50, p90) in tonnes/day."""
    fn = dict(gbm=gbm_predict, lstm=lstm_predict, prophet=prophet_predict)[family]
    p10, p50, p90 = fn(model, rows, hist)
    cap = rows.capacity_tpd.values
    p10, p50, p90 = p10 * cap, p50 * cap, p90 * cap
    return np.minimum(p10, p50), p50, np.maximum(p90, p50)


def available():
    out = ["gbm"]
    try:
        import torch  # noqa: F401
        out.append("lstm")
    except ImportError:
        pass
    try:
        import prophet  # noqa: F401
        out.append("prophet")
    except ImportError:
        pass
    return out
