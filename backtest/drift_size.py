"""E41: drift trade size on the $100,000 scored bankroll.

    .venv/bin/python -m backtest.drift_size

Drift's daily P&L at $25k per trade (v2b, 15 s checks, recorded par data,
as in E38) is rescaled to other trade sizes and to half its P&L (live
catches less than the backtest, E30); 18-day competitions are simulated by
resampling those days. Score proxy as E5/E38 (two guesses of the field),
and Q, the official score's financial part (E43): Q = (max(R,0) + 0.005)
/ (|R| + 1.5 sigma + 0.05), sigma = daily std x sqrt(days).
"""
import numpy as np
import pandas as pd

from .engine import score
from .rp_role import FIELDS, drift_daily

EQUITY = 100_000
DAYS = 18                       # Oct 19 12:00 -> Nov 6 12:00 UTC
SIZES = [2_500, 5_000, 10_000, 25_000, 50_000]
DRAWS = 3000


def official_q(r, sigma):
    return (max(r, 0) + 0.005) / (abs(r) + 1.5 * sigma + 0.05)


def main():
    rng = np.random.default_rng(0)
    tr, dd = drift_daily()
    print(f"Drift @ $25k: {len(tr)} trades, worst trade ${tr.net.min():,.0f}; daily mean "
          f"${dd.mean():,.0f}, std ${dd.std():,.0f}, worst day ${dd.min():,.0f}\n")
    rows = []
    for size in SIZES:
        for pnl_x in (1.0, 0.5, 0.25):
            k = size / 25_000 * pnl_x
            sims = []
            for _ in range(DRAWS):
                d = k * rng.choice(dd.to_numpy(), DAYS) / EQUITY
                r, sd = np.prod(1 + d) - 1, d.std(ddof=1)
                sims.append({"total_ret_%": 100 * r, "daily_std_%": 100 * sd,
                             "Q": official_q(r, sd * np.sqrt(DAYS))})
            t = pd.DataFrame(sims)
            rows.append({"size_$": size, "pnl_x": pnl_x, "ret_med_%": t["total_ret_%"].median(),
                         "ret_p10_%": t["total_ret_%"].quantile(0.1), "std_med_%": t["daily_std_%"].median(),
                         "worst_trade_%eq": 100 * k * tr.net.min() / EQUITY,
                         **{f"score_{n}": score(t, r, s).mean() for n, (r, s) in FIELDS.items()},
                         "Q_mean": t.Q.mean(), "Q_p10": t.Q.quantile(0.1)})
    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", "{:.2f}".format)
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
