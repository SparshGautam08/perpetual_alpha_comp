"""Step 6: leverage vs volatility tradeoff.

    .venv/bin/python -m backtest.leverage_study

Candidates are picked on in-sample (Apr 18 - Jul 31) score only:
- trend_LS_10v on CRYPTO3: best in-sample absolute score.
- risk_parity_10v on CLEAN7: most consistent across both periods.

Scoring: the competition is weeks long, so besides the full periods we
score every 14-day window and average. Absolute score ranges are guesses
at the field's spread, so we show two of them.
"""
import numpy as np
import pandas as pd

from .data import CLEAN, CRYPTO, MARKETS, load_panel
from .engine import backtest, metrics, score
from .run_baselines import IS_END, OOS_START, WARM_START, make_strategies

LEVERAGES = [0.5, 1, 1.5, 2, 3, 4, 5]
# Assumed field ranges for one 14-day window: (return %, daily std %).
FIELDS = {"narrow": ((-5, 10), (0, 2)), "wide": ((-15, 30), (0, 4))}


def window_scores(res, start, end, days=14):
    """Average score over every 14-day window in [start, end]."""
    daily = res.equity.loc[start:end].resample("D").last()
    rows = []
    for i in range(len(daily) - days):
        w = daily.iloc[i:i + days + 1]
        dr = w.pct_change().dropna()
        rows.append({"total_ret_%": 100 * (w.iloc[-1] / w.iloc[0] - 1),
                     "daily_std_%": 100 * dr.std()})
    t = pd.DataFrame(rows)
    out = {"14d_ret_med_%": t["total_ret_%"].median(),
           "14d_ret_p10_%": t["total_ret_%"].quantile(0.1),
           "14d_std_med_%": t["daily_std_%"].median()}
    for name, (rr, sr) in FIELDS.items():
        out[f"score14_{name}"] = score(t, rr, sr).mean()
    return out


def study(label, universe, strat):
    _, ret, live, _ = load_panel(MARKETS)
    ret, live = ret[universe], live[universe]
    w, sched = make_strategies(ret, live)[strat]
    rows = []
    for lev in LEVERAGES:
        res = backtest(w, ret, sched, leverage=lev, band=0.01)
        for period, (a, b) in {"IS": (WARM_START, IS_END), "OOS": (OOS_START, None)}.items():
            m = metrics(res, a, b)
            rows.append({"lev": lev, "period": period,
                         "ret_%": m["total_ret_%"], "daily_std_%": m["daily_std_%"],
                         "max_dd_%": m["max_dd_%"], "worst_day_%": m["worst_day_%"],
                         "gross": m["avg_gross"], "costs_%": m["fees_%"] + m["spread_%"],
                         "funding_%": m["funding_%"]} | window_scores(res, a, b))
    df = pd.DataFrame(rows)
    print(f"\n=== {label}: {strat} ===")
    for p in ["IS", "OOS"]:
        print(f"-- {p} --")
        print(df[df.period == p].drop(columns="period").to_string(index=False))
    return df


def main():
    pd.set_option("display.width", 220)
    pd.set_option("display.float_format", "{:.2f}".format)
    study("CRYPTO3", CRYPTO, "trend_LS_10v")
    study("CLEAN7", CLEAN, "risk_parity_10v")


if __name__ == "__main__":
    main()
