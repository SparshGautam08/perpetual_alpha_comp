"""E23: ML Model 1, forecast par's oracle over the next H seconds.

    .venv/bin/python -m backtest.ml_forecast

Target: par oracle log change over the next H s (bp), H = 15 (also 5, 30).
Features at second t (only data up to t):
- prem_raw: mainnet mid vs par oracle (bp); prem_dm: minus its rolling
  30-min median (persistent offset removed); prem_dm 1/3/5/10 s ago
- mainnet mid returns over the last 1/5/15/60 s; mainnet 5-min volatility
- par oracle returns over the last 5/15/60 s; seconds since it last changed
- par book mid vs oracle; mainnet's own oracle vs par oracle
- hour of day (UTC), US equity session flag, market id
Models (same features, same folds):
- baseline: OLS on prem_dm only (the E20 rule)
- linear: ridge regression on all features
- gbm: HistGradientBoostingRegressor on all features
Walk-forward: expanding training window, 6 h test blocks, a 5 min purge
gap between train and test. All metrics are out of sample.
"""
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import LinearRegression, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .lead_lag import PAIRS, _read

ROOT = Path(__file__).resolve().parent.parent
DESIGN_END = pd.Timestamp("2026-10-09 23:09:00", tz="UTC")
HORIZONS = [5, 15, 30]
MAIN_H = 15
TEST_BLOCK = pd.Timedelta(hours=6)
FIRST_TRAIN = pd.Timedelta(hours=24)
PURGE = pd.Timedelta(minutes=5)
from bot.features import FEATURES, PAIRS as _PAIRS, compute_features  # shared with live


def build(end=DESIGN_END) -> pd.DataFrame:
    par = _read("data_ws/ctxws_*.csv", ["recv_ms", "coin", "oraclePx", "midPx"])
    mb = _read("data_mainnet/mn_bbo_*.csv", ["recv_ms", "coin", "bid_px", "ask_px"])
    mc = _read("data_mainnet/mn_ctxws_*.csv", ["recv_ms", "coin", "oraclePx"])
    t0 = max(par.recv_ms.min(), mb.recv_ms.min())
    t1 = min(par.recv_ms.max(), mb.recv_ms.max())
    grid = pd.date_range(pd.Timestamp(t0, unit="ms", tz="UTC").ceil("s"),
                         pd.Timestamp(t1, unit="ms", tz="UTC").floor("s"), freq="s")
    ff = lambda df, col: (df.assign(t=pd.to_datetime(df.recv_ms, unit="ms", utc=True))
                          .drop_duplicates("t", keep="last").set_index("t")[col].sort_index()
                          .reindex(grid, method="ffill"))
    frames = []
    for k, (coin, mn) in enumerate(PAIRS.items()):
        p = par[par.coin == f"par:{coin}"]
        m = mb[mb.coin == mn].assign(mid=lambda d: (d.bid_px + d.ask_px) / 2)
        mo = mc[mc.coin == mn]
        g = pd.DataFrame({"o": ff(p, "oraclePx"), "pmid": ff(p, "midPx"),
                          "m": ff(m, "mid"), "mo": ff(mo, "oraclePx")}).dropna()
        f = compute_features(g, coin)
        lo = np.log(g.o)
        for h in HORIZONS:
            f[f"y_{h}"] = (lo.shift(-h) - lo) * 1e4
        f["coin"] = coin
        frames.append(f)
    df = pd.concat(frames).dropna(subset=FEATURES + [f"y_{h}" for h in HORIZONS])
    return df[df.index <= end]


def folds(index: pd.DatetimeIndex):
    start, end = index.min(), index.max()
    cut = start + FIRST_TRAIN
    while cut + PURGE < end:
        test_end = min(cut + PURGE + TEST_BLOCK, end)
        yield cut, cut + PURGE, test_end
        cut = test_end


def models():
    return {
        "baseline": ("prem_dm only", lambda: LinearRegression()),
        "linear": ("all", lambda: make_pipeline(StandardScaler(), Ridge(alpha=1.0))),
        "gbm": ("all", lambda: HistGradientBoostingRegressor(
            max_iter=300, learning_rate=0.05, max_leaf_nodes=31, min_samples_leaf=200,
            l2_regularization=1.0, categorical_features=[FEATURES.index("market")],
            random_state=0)),
    }


def evaluate(y, pred) -> dict:
    """Out-of-sample metrics. 'top10_move_bp' = average realized oracle move
    in the predicted direction for the 10% most confident predictions."""
    r2 = 1 - ((y - pred) ** 2).sum() / ((y - y.mean()) ** 2).sum()
    q = np.quantile(np.abs(pred), 0.9)
    top = np.abs(pred) >= q
    moved = top & (y != 0)
    return {"r2": r2, "corr": np.corrcoef(y, pred)[0, 1],
            "hit_top10_%": 100 * (np.sign(pred[moved]) == np.sign(y[moved])).mean(),
            "top10_move_bp": (np.sign(pred[top]) * y[top]).mean(),
            "top1_move_bp": (np.sign(pred) * y)[np.abs(pred) >= np.quantile(np.abs(pred), 0.99)].mean()}


def main():
    pd.set_option("display.width", 220)
    pd.set_option("display.float_format", "{:.3f}".format)
    df = build()
    print(f"Rows {len(df):,} ({df.index.min():%m-%d %H:%M} -> {df.index.max():%m-%d %H:%M} UTC), "
          f"{df.coin.nunique()} markets, {len(FEATURES)} features\n")
    results, last_gbm = [], None
    for h in HORIZONS:
        y_col = f"y_{h}"
        for i, (tr_end, te_start, te_end) in enumerate(folds(df.index)):
            tr = df[df.index < tr_end]
            te = df[(df.index >= te_start) & (df.index < te_end)]
            for name, (cols, make) in models().items():
                X_cols = ["prem_dm"] if cols == "prem_dm only" else FEATURES
                m = make().fit(tr[X_cols], tr[y_col])
                pred = m.predict(te[X_cols])
                results.append({"H_s": h, "fold": i, "test_from": te_start.strftime("%m-%d %H:%M"),
                                "model": name, "n_test": len(te)} | evaluate(te[y_col].to_numpy(), pred))
                if name == "gbm" and h == MAIN_H:
                    last_gbm = (m, te, pred)
    r = pd.DataFrame(results)
    r.to_csv(ROOT / "results" / "ml_forecast_walkforward.csv", index=False)
    print("=== Walk-forward, out of sample, averaged over folds ===")
    print(r.groupby(["H_s", "model"])[["r2", "corr", "hit_top10_%", "top10_move_bp", "top1_move_bp"]]
          .mean().to_string())
    print("\n=== Per fold, H = 15 s ===")
    print(r[r.H_s == MAIN_H].pivot_table(index=["fold", "test_from"], columns="model", values="r2").to_string())
    print("\n=== Per market, H = 15 s, last fold, gbm vs baseline (corr) ===")
    m, te, pred = last_gbm
    base = LinearRegression().fit(df[df.index < te.index.min() - PURGE][["prem_dm"]],
                                  df[df.index < te.index.min() - PURGE][f"y_{MAIN_H}"])
    te = te.assign(pred_gbm=pred, pred_base=base.predict(te[["prem_dm"]]))
    print(te.groupby("coin").apply(lambda g: pd.Series({
        "gbm_corr": np.corrcoef(g[f"y_{MAIN_H}"], g.pred_gbm)[0, 1],
        "base_corr": np.corrcoef(g[f"y_{MAIN_H}"], g.pred_base)[0, 1]})).to_string())


if __name__ == "__main__":
    main()
