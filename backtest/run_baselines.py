"""Run baseline strategies on the historical candles.

    .venv/bin/python -m backtest.run_baselines

Periods: all strategies trade from WARM_START (30 days after SP500 listed,
so every market has enough history), in-sample to IS_END, out-of-sample
after. Trend lookbacks are tuned on in-sample only.
"""
from pathlib import Path

import pandas as pd

from .data import CLEAN, CRYPTO, MARKETS, load_panel
from .engine import backtest, daily_schedule, metrics, rolling_windows
from .strategies import equal_weight, ewma_cov, risk_parity, trend

WARM_START = "2026-04-18"
IS_END = "2026-07-31 23:00"
OOS_START = "2026-08-01"
OUT = Path(__file__).resolve().parent.parent / "results"

pd.set_option("display.width", 200)
pd.set_option("display.float_format", "{:.2f}".format)


def run_all(universe, fee_bps=5.0, slip_bps=2.0, trend_lbs=(24 * 7, 24 * 30)):
    close, ret, live, _ = load_panel(MARKETS)
    ret, live = ret[universe], live[universe]
    trade_ok = pd.Series(ret.index >= pd.Timestamp(WARM_START, tz="UTC"), ret.index)
    daily = daily_schedule(ret.index) & trade_ok
    once = pd.Series(False, ret.index)
    once[daily.idxmax()] = True
    cov = ewma_cov(ret, 24 * 7)

    strats = {
        "buy_hold_eq": (equal_weight(ret, live), once),
        "eq_weight_daily": (equal_weight(ret, live), daily),
        "risk_parity_10v": (risk_parity(ret, live, 0.10, cov=cov), daily),
        "risk_parity_20v": (risk_parity(ret, live, 0.20, cov=cov), daily),
        "trend_LS_10v": (trend(ret, live, trend_lbs, 0.10, cov=cov), daily),
        "trend_LO_10v": (trend(ret, live, trend_lbs, 0.10, long_only=True, cov=cov), daily),
    }
    rows = []
    for name, (w, sched) in strats.items():
        res = backtest(w, ret, sched, fee_bps, slip_bps, band=0.01)
        for period, (a, b) in {"IS": (WARM_START, IS_END),
                               "OOS": (OOS_START, None)}.items():
            m = metrics(res, a, b) | rolling_windows(res, 14, a, b)
            rows.append({"strategy": name, "period": period} | m)
    return pd.DataFrame(rows)


def trend_sweep(universe):
    """In-sample only: pick lookbacks without peeking at OOS."""
    _, ret, live, _ = load_panel(MARKETS)
    ret, live = ret[universe], live[universe]
    trade_ok = pd.Series(ret.index >= pd.Timestamp(WARM_START, tz="UTC"), ret.index)
    daily = daily_schedule(ret.index) & trade_ok
    cov = ewma_cov(ret, 24 * 7)
    rows = []
    for lbs in [(24,), (24 * 3,), (24 * 7,), (24 * 14,), (24 * 30,),
                (24 * 7, 24 * 30), (24 * 3, 24 * 14), (24, 24 * 7, 24 * 30)]:
        res = backtest(trend(ret, live, lbs, 0.10, cov=cov), ret, daily, band=0.01)
        rows.append({"lookbacks_h": lbs} | metrics(res, WARM_START, IS_END))
    return pd.DataFrame(rows)


def main():
    OUT.mkdir(exist_ok=True)
    cols = ["strategy", "period", "total_ret_%", "ann_ret_%", "daily_std_%",
            "sharpe", "max_dd_%", "worst_day_%", "avg_gross", "fees_%",
            "14d_ret_med_%", "14d_ret_p10_%", "14d_pct_positive"]

    _, ret, _, _ = load_panel(MARKETS)
    d = (1 + ret.loc[WARM_START:]).resample("D").prod() - 1
    print("\n=== Daily return correlations (Apr 18 - Oct 5) ===")
    print(d.corr().round(2))
    print("\n=== Annualized vol by market (daily returns) ===")
    print((d.std() * 365 ** 0.5 * 100).round(1).to_string())

    print("\n=== Trend lookback sweep (in-sample only, all 10 markets) ===")
    sw = trend_sweep(MARKETS)
    print(sw[["lookbacks_h", "total_ret_%", "daily_std_%", "sharpe", "max_dd_%",
              "fees_%"]].to_string(index=False))
    sw.to_csv(OUT / "trend_sweep_IS.csv", index=False)

    for label, uni in [("ALL10", MARKETS), ("CLEAN7", CLEAN), ("CRYPTO3", CRYPTO)]:
        df = run_all(uni)
        df.insert(0, "universe", label)
        df.to_csv(OUT / f"baselines_{label}.csv", index=False)
        print(f"\n=== Baselines: {label} {uni} (fee 5bp + slip 2bp) ===")
        print(df[cols].to_string(index=False))

    print("\n=== Fee sensitivity, ALL10, out-of-sample ===")
    rows = []
    for fee in [0.0, 5.0, 10.0, 20.0]:
        df = run_all(MARKETS, fee_bps=fee, slip_bps=0.0)
        df = df[df.period == "OOS"][["strategy", "total_ret_%", "sharpe", "fees_%"]]
        df.insert(0, "cost_bps", fee)
        rows.append(df)
    print(pd.concat(rows).to_string(index=False))


if __name__ == "__main__":
    main()
