"""E46: funding carry on pas: hold the side that RECEIVES funding.

    .venv/bin/python -m backtest.carry_study

pas funding has been one-sided since the start (longs paid ~65-100%/yr on
SP500 and crypto, shorts paid ~34%/yr on USDJPY), as on par for the week
before (E46 notes). Funding history on pas is too short to backtest its
persistence, so the study separates the two risks:
- PRICE risk: real mainnet history (organizers' hourly candles, Apr 18 ->
  Oct 5), every 16-day window (the rest of the competition), with the price
  DRIFT REMOVED (zero expected price return, so no credit for the bull run);
- FUNDING: assumed constant at FUNDING_SCALE x the pas average so far
  (1 = as observed, 0.5, 0 = it disappears, -0.5 = it flips against us).
Costs: taker fee + half spread on entry and exit (2 x 15 bp of notional).
Score = the official Q (E43) on the window's 16 daily returns.

Portfolios (on $100,000):
- SP500: long SP500 only.
- SP500+EWY: long SP500, short EWY at the variance-minimizing hedge ratio.
- BASKET: mean-variance carry weights (Sigma^-1 x carry) over the markets
  with |funding| >= 20%/yr, scaled to a target daily vol.
Sizes are given as $ (single) or as the basket's daily vol target.
"""
import numpy as np
import pandas as pd

from .data import load_panel
from .drift_size import official_q

EQUITY = 100_000
DAYS = 16
COST_BP = 30                     # round trip: 2 x (9 bp taker + ~6 bp half spread)
# pas average funding Oct 19 12:00 -> Oct 21 00:00 UTC, %/yr (+ = longs pay)
PAS_FUNDING = {"BTC": -68.6, "ETH": -64.0, "HYPE": -66.9, "SP500": -102.2, "EWY": 3.8,
               "EWJ": -12.3, "BRENT": 5.3, "GOLD": 5.3, "USDEUR": -18.0, "USDJPY": 34.1}
SCALES = [1.0, 0.5, 0.0, -0.5]


def daily_returns():
    _, ret, _, _ = load_panel()
    d = (1 + ret.loc["2026-04-18":]).resample("D").prod() - 1
    return d - d.mean()                       # remove price drift


def windows(d):
    return [d.iloc[i:i + DAYS] for i in range(len(d) - DAYS + 1)]


def evaluate(d, weights_usd, scale):
    """weights_usd: {market: signed $ notional}. Returns per-window R, sigma, Q."""
    w = pd.Series(weights_usd, dtype=float)
    carry_daily = scale * sum(-PAS_FUNDING[m] / 100 / 365 * n for m, n in w.items())   # longs receive when f < 0
    gross = w.abs().sum()
    rows = []
    for win in windows(d[w.index]):
        pnl = win.to_numpy() @ w.to_numpy() + carry_daily
        pnl[0] -= COST_BP / 2 / 1e4 * gross
        pnl[-1] -= COST_BP / 2 / 1e4 * gross
        r = pnl / EQUITY
        R = np.prod(1 + r) - 1
        sig = r.std(ddof=1) * np.sqrt(DAYS)
        rows.append((R, sig, official_q(R, sig)))
    t = pd.DataFrame(rows, columns=["R", "sigma", "Q"])
    return {"R_med_%": 100 * t.R.median(), "R_p10_%": 100 * t.R.quantile(0.1), "P(loss)_%": 100 * (t.R < 0).mean(),
            "sigma_med_%": 100 * t.sigma.median(), "Q_mean": t.Q.mean(), "Q_p10": t.Q.quantile(0.1)}


def basket(d, vol_target_daily):
    mk = [m for m, f in PAS_FUNDING.items() if abs(f) >= 20]
    cov = d[mk].cov().to_numpy()
    mu = np.array([-PAS_FUNDING[m] / 100 / 365 for m in mk])      # carry per $ per day, side-adjusted
    raw = np.linalg.solve(cov, mu)
    vol = np.sqrt(raw @ cov @ raw)
    return dict(zip(mk, raw / vol * vol_target_daily * EQUITY))


def main():
    d = daily_returns()
    pd.set_option("display.width", 220)
    pd.set_option("display.float_format", "{:.2f}".format)
    h = d["SP500"].cov(d["EWY"]) / d["EWY"].var()
    print(f"{len(windows(d))} windows of {DAYS} days (price drift removed); SP500/EWY hedge ratio {h:.3f}")
    b = basket(d, 0.005)
    print("BASKET weights at 0.5%/day vol ($): " + ", ".join(f"{m} {v:+,.0f}" for m, v in b.items()))
    portfolios = {}
    for n in (10_000, 25_000, 50_000, 100_000):
        portfolios[f"SP500 long ${n // 1000}k"] = {"SP500": n}
        portfolios[f"SP500+EWY ${n // 1000}k"] = {"SP500": n, "EWY": -h * n}
    for v in (0.0025, 0.005, 0.01):
        portfolios[f"BASKET {100 * v:.2f}%/day"] = basket(d, v)
    rows = []
    for name, w in portfolios.items():
        for s in SCALES:
            rows.append({"portfolio": name, "funding_x": s, "gross_$k": sum(abs(x) for x in w.values()) / 1e3,
                         **evaluate(d, w, s)})
    print(pd.DataFrame(rows).to_string(index=False))
    print(f"\nReference: flat (R = 0) -> Q = {official_q(0, 0):.3f}")


if __name__ == "__main__":
    main()
