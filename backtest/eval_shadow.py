"""Score shadow-mode predictions (bot/shadow.py) against what par's oracle
actually did over the next 15 s.

    .venv/bin/python -m backtest.eval_shadow [folder_with_shadow_csvs]

Realized move = log(oracle at t+15 / oracle at t) in bp, taken from the
shadow log itself (one row per market per second). Compares R2 and
correlation of the GBM vs the baseline, overall and per market, to the
out-of-sample test (E27: GBM 0.32, baseline 0.16).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

H = 15


def r2(y, p):
    return 1 - ((y - p) ** 2).sum() / ((y - y.mean()) ** 2).sum()


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "shadow"
    df = pd.concat([pd.read_csv(f) for f in sorted(folder.glob("shadow_*.csv"))], ignore_index=True)
    rows = []
    for c, g in df.groupby("coin"):
        g = g.drop_duplicates("t").set_index("t").sort_index()
        fut = g.oracle.reindex(g.index + H)
        g = g.assign(y=(np.log(fut.to_numpy()) - np.log(g.oracle.to_numpy())) * 1e4).dropna(subset=["y"])
        rows.append(g.assign(coin=c))
    d = pd.concat(rows)
    t0, t1 = pd.to_datetime(d.index.min(), unit="s"), pd.to_datetime(d.index.max(), unit="s")
    print(f"Shadow predictions scored: {len(d):,} ({t0:%m-%d %H:%M} -> {t1:%m-%d %H:%M} UTC, "
          f"{(t1 - t0).total_seconds() / 3600:.1f} h)\n")
    print(f"ALL:  R2 GBM {r2(d.y, d.pred_gbm_bp):.3f}  vs baseline {r2(d.y, d.pred_base_bp):.3f}   "
          f"corr GBM {np.corrcoef(d.y, d.pred_gbm_bp)[0, 1]:.3f}  baseline {np.corrcoef(d.y, d.pred_base_bp)[0, 1]:.3f}")
    print("      (out-of-sample test E27: GBM 0.324, baseline 0.160)\n")
    per = d.groupby("coin").apply(lambda g: pd.Series({
        "n": len(g), "R2_gbm": r2(g.y, g.pred_gbm_bp), "R2_base": r2(g.y, g.pred_base_bp),
        "mean_abs_move_bp": g.y.abs().mean()}))
    pd.set_option("display.float_format", "{:.3f}".format)
    print(per.to_string())


if __name__ == "__main__":
    main()
