"""E39: does ML Model 1 make drift pick better trades? (locked test)

    .venv/bin/python -m backtest.ml_drift_filter

GBM (E23 settings) trained only on data up to DESIGN_END (Oct 9 23:09 UTC).
On the data after it, drift v2b at the live check speed (15 s) runs twice
on the same rows: plain, and with entries judged on the edge adjusted by
the model's predicted 15 s oracle move (short: sell_edge - p, long:
buy_edge + p). Criterion (locked in REPORT_NOTES E39): filtered net >
plain net.
"""
import numpy as np
import pandas as pd

from . import ml_forecast as M
from .drift_strategy import prepare, run_v2, simulate_v2
from .live_market import load

CHECK_S = 15
DRIFT = {"entry_edge_bp": 30, "exit_cost_bp": 10, "max_hold_h": 12, "need_funding": False,
         "stop_bp": 200}


def summary(tr: pd.DataFrame) -> dict:
    return {"trades": len(tr), "win_%": 100 * (tr.net > 0).mean() if len(tr) else np.nan,
            "net_$": tr.net.sum(), "excl_oracle_$": (tr.net - tr.oracle_move).sum(),
            "edge_in_$": tr.edge_in.sum(), "edge_out_$": tr.edge_out.sum(),
            "oracle_move_$": tr.oracle_move.sum(), "fees_$": tr.fees.sum()}


def main():
    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", "{:.1f}".format)

    # 1. Model trained on design data only; predictions on the test period.
    feat = M.build(end=pd.Timestamp("2100-01-01", tz="UTC"))
    train, test = feat[feat.index <= M.DESIGN_END], feat[feat.index > M.DESIGN_END]
    gbm = M.models()["gbm"][1]().fit(train[M.FEATURES], train[f"y_{M.MAIN_H}"])
    test = test.assign(pred=gbm.predict(test[M.FEATURES]))
    y = test[f"y_{M.MAIN_H}"]
    r2 = 1 - ((y - test.pred) ** 2).sum() / ((y - y.mean()) ** 2).sum()
    print(f"Model trained on {train.index.min():%m-%d %H:%M} -> {train.index.max():%m-%d %H:%M} UTC "
          f"({len(train):,} rows); test R2 {r2:.3f} on {len(test):,} rows "
          f"({test.index.min():%m-%d %H:%M} -> {test.index.max():%m-%d %H:%M})\n")
    preds = {c: g.pred for c, g in test.groupby("coin")}

    # 2. Drift on the test period at the live check speed, plain vs filtered.
    df = load()
    df = df[df.time > M.DESIGN_END]
    df = df.groupby(["coin", df.ts_ms // (CHECK_S * 1000)]).head(1)
    prep = prepare(df)
    plain = run_v2(prep, DRIFT)
    rows, missing, checks = [], 0, 0
    for coin, (g, b, s) in prep.items():
        p = preds[coin].reindex(g.time.dt.floor("s")).to_numpy() if coin in preds else np.full(len(g), np.nan)
        missing += int(np.isnan(p).sum())
        checks += len(g)
        for tr in simulate_v2(g, b, s, pred=p, **DRIFT):
            rows.append({"coin": coin} | tr)
    filt = pd.DataFrame(rows)
    print(f"Drift v2b @ {CHECK_S} s, {df.time.min():%m-%d %H:%M} -> {df.time.max():%m-%d %H:%M} UTC; "
          f"checks without a prediction: {missing:,} of {checks:,} ({100 * missing / checks:.1f}%)\n")
    print(pd.DataFrame({"plain": summary(plain), "ML filter": summary(filt)}).to_string())

    # 3. Which trades changed.
    key = lambda t: set(zip(t.coin, pd.to_datetime(t.t0)))
    kp, kf = key(plain), key(filt)
    skipped = plain[[k not in kf for k in zip(plain.coin, pd.to_datetime(plain.t0))]]
    added = filt[[k not in kp for k in zip(filt.coin, pd.to_datetime(filt.t0))]]
    print(f"\nPlain trades the filter skipped: {len(skipped)} (their net ${skipped.net.sum():,.0f}); "
          f"trades only the filter took: {len(added)} (net ${added.net.sum():,.0f})")
    print("\nPer market (net $):")
    print(pd.DataFrame({"plain": plain.groupby("coin").net.sum(),
                        "ML filter": filt.groupby("coin").net.sum()}).fillna(0).to_string())
    diff = filt.net.sum() - plain.net.sum()
    print(f"\nCRITERION (filtered net > plain net): {filt.net.sum():,.0f} vs {plain.net.sum():,.0f} "
          f"-> {'PASSED' if diff > 0 else 'FAILED'} ({diff:+,.0f})")


if __name__ == "__main__":
    main()
