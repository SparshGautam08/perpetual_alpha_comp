"""Run baseline strategies on the historical candles.

    .venv/bin/python -m backtest.run_baselines

Periods: all strategies trade from WARM_START (30 days after SP500 listed,
so every market has enough history), in-sample to IS_END, out-of-sample
after. Costs (fee, spread, funding) come from costs.py.
"""
from pathlib import Path

import pandas as pd

from .data import CLEAN, CRYPTO, MARKETS, load_panel
from .engine import backtest, daily_schedule, metrics, rolling_windows, score
from .strategies import equal_weight, ewma_cov, risk_parity, trend

WARM_START = "2026-04-18"
IS_END = "2026-07-31 23:00"
OOS_START = "2026-08-01"
PERIODS = {"IS": (WARM_START, IS_END), "OOS": (OOS_START, None)}
OUT = Path(__file__).resolve().parent.parent / "results"

pd.set_option("display.width", 220)
pd.set_option("display.float_format", "{:.2f}".format)


def make_strategies(ret, live):
    trade_ok = pd.Series(ret.index >= pd.Timestamp(WARM_START, tz="UTC"), ret.index)
    daily = daily_schedule(ret.index) & trade_ok
    once = pd.Series(False, ret.index)
    once[daily.idxmax()] = True
    cov = ewma_cov(ret, 24 * 7)
    return {
        "buy_hold_eq": (equal_weight(ret, live), once),
        "eq_weight_daily": (equal_weight(ret, live), daily),
        "risk_parity_10v": (risk_parity(ret, live, 0.10, cov=cov), daily),
        "risk_parity_20v": (risk_parity(ret, live, 0.20, cov=cov), daily),
        "trend_LS_10v": (trend(ret, live, (24 * 7, 24 * 30), 0.10, cov=cov), daily),
        "trend_LO_10v": (trend(ret, live, (24 * 7, 24 * 30), 0.10,
                               long_only=True, cov=cov), daily),
    }


def run_all(universe, **cost_kw):
    _, ret, live, _ = load_panel(MARKETS)
    ret, live = ret[universe], live[universe]
    rows = []
    for name, (w, sched) in make_strategies(ret, live).items():
        res = backtest(w, ret, sched, band=0.01, **cost_kw)
        for period, (a, b) in PERIODS.items():
            m = metrics(res, a, b) | rolling_windows(res, 14, a, b)
            rows.append({"strategy": name, "period": period} | m)
    df = pd.DataFrame(rows)
    # Relative score: rank strategies against each other within a period.
    df["score_rel"] = df.groupby("period", group_keys=False).apply(score)
    # Absolute score: assumed field from -10%..+20% return, 0..2% daily std.
    df["score_abs"] = score(df, ret_range=(-10, 20), std_range=(0, 2))
    return df


def trend_sweep(universe):
    """In-sample only: pick lookbacks without peeking at OOS."""
    _, ret, live, _ = load_panel(MARKETS)
    ret, live = ret[universe], live[universe]
    sched = make_strategies(ret, live)["eq_weight_daily"][1]
    cov = ewma_cov(ret, 24 * 7)
    rows = []
    for lbs in [(24,), (24 * 3,), (24 * 7,), (24 * 14,), (24 * 30,),
                (24 * 7, 24 * 30), (24 * 3, 24 * 14), (24, 24 * 7, 24 * 30)]:
        res = backtest(trend(ret, live, lbs, 0.10, cov=cov), ret, sched, band=0.01)
        rows.append({"lookbacks_h": lbs} | metrics(res, WARM_START, IS_END))
    return pd.DataFrame(rows)


def main():
    OUT.mkdir(exist_ok=True)
    print("\n=== Trend lookback sweep (in-sample only, all 10 markets) ===")
    sw = trend_sweep(MARKETS)
    sw.to_csv(OUT / "trend_sweep_IS.csv", index=False)
    print(sw[["lookbacks_h", "total_ret_%", "daily_std_%", "sharpe",
              "max_dd_%", "fees_%", "spread_%"]].to_string(index=False))
    cols = ["strategy", "period", "total_ret_%", "daily_std_%", "max_dd_%",
            "sharpe", "avg_gross", "fees_%", "spread_%", "funding_%",
            "14d_ret_p10_%", "score_rel", "score_abs"]

    for label, uni in [("ALL10", MARKETS), ("CLEAN7", CLEAN), ("CRYPTO3", CRYPTO)]:
        df = run_all(uni)
        df.insert(0, "universe", label)
        df.to_csv(OUT / f"baselines_{label}.csv", index=False)
        print(f"\n=== Baselines: {label} (costs from costs.py) ===")
        print(df[cols].to_string(index=False))

    print("\n=== Cost ablation, ALL10, out-of-sample: total return % ===")
    variants = {
        "no costs": dict(fee_bps=0, half_spread_bps=0, funding=None),
        "fee only": dict(half_spread_bps=0, funding=None),
        "fee+spread": dict(funding=None),
        "fee+spread+funding": {},
    }
    abl = {}
    for vname, kw in variants.items():
        df = run_all(MARKETS, **kw)
        abl[vname] = df[df.period == "OOS"].set_index("strategy")["total_ret_%"]
    print(pd.DataFrame(abl).to_string())

    print("\n=== Leverage, risk_parity_10v, ALL10, out-of-sample ===")
    _, ret, live, _ = load_panel(MARKETS)
    w, sched = make_strategies(ret, live)["risk_parity_10v"]
    rows = []
    for lev in [0.5, 1.0, 2.0, 3.0]:
        res = backtest(w, ret, sched, leverage=lev, band=0.01)
        rows.append({"leverage": lev} | metrics(res, OOS_START, None))
    print(pd.DataFrame(rows)[["leverage", "total_ret_%", "daily_std_%",
                              "max_dd_%", "avg_gross", "spread_%",
                              "funding_%"]].to_string(index=False))


if __name__ == "__main__":
    main()
