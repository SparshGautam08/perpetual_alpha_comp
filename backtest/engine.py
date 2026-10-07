"""Hourly portfolio simulator.

How to use it:
    res = backtest(weights, ret, rebalance)        # defaults from costs.py
    print(metrics(res, "2026-08-01", None))

Inputs
- weights: DataFrame [hour x market] of target positions as signed
  notional / equity. 0.5 = long half your equity, -0.2 = short 20%.
  sum(|w|) is gross leverage.
- ret: hourly simple returns per market (from data.load_panel).
- rebalance: bool Series, True on hours where we trade to target.

No lookahead: weights[t] may only use data up to the close of hour t;
they are applied at that close and earn the return of hour t+1.

Each hour, in order:
1. Positions earn that hour's price return and drift with it.
2. Funding is charged on the position (longs pay when the rate is > 0).
3. On rebalance hours, trade to target. Each trade pays
   fee_bps + half_spread_bps of its notional.

Prices are the dataset's mainnet trade prices, used as a proxy for both
the oracle (what we are scored on) and the mid we trade around.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import costs

HOURS_PER_YEAR = 24 * 365


@dataclass
class Result:
    equity: pd.Series      # hourly equity, starts at 1.0
    gross: pd.Series       # gross leverage after trading
    turnover: pd.Series    # traded notional / equity per hour
    fees: pd.Series        # exchange fees, fraction of equity per hour
    spread: pd.Series      # spread cost, fraction of equity per hour
    funding: pd.Series     # funding paid (+) or received (-), per hour


def daily_schedule(index: pd.DatetimeIndex, hour: int = 0) -> pd.Series:
    """Rebalance once a day at `hour` UTC."""
    return pd.Series(index.hour == hour, index=index)


def _per_market(value, markets, default=0.0) -> np.ndarray:
    """Accept a number (same for all markets) or a {market: value} dict."""
    if isinstance(value, dict):
        return np.array([value.get(m, default) for m in markets], dtype=float)
    return np.full(len(markets), float(value))


def backtest(weights: pd.DataFrame, ret: pd.DataFrame, rebalance: pd.Series,
             fee_bps=costs.FEE_BPS,
             half_spread_bps=costs.HALF_SPREAD_BPS,
             funding=costs.FUNDING_APR,
             leverage: float = 1.0,
             max_gross: float | None = None,
             band: float = 0.0) -> Result:
    """Simulate a strategy.

    fee_bps, half_spread_bps: number or {market: bp}.
    funding: None (off), a number or {market: annual rate} (constant), or a
        DataFrame [hour x market] of per-hour rates (e.g. real recorded
        funding). Positive rate = longs pay.
    leverage: multiplies every target weight (2.0 = twice the size).
    max_gross: if set, scale targets down so sum(|w|) <= max_gross.
    band: skip a market's trade if |target - current| < band * equity
        (saves costs on tiny adjustments).
    """
    mk = list(ret.columns)
    w = weights.reindex(index=ret.index, columns=mk).fillna(0.0).to_numpy() * leverage
    if max_gross is not None:
        g = np.abs(w).sum(axis=1, keepdims=True)
        w = np.where(g > max_gross, w * max_gross / np.maximum(g, 1e-12), w)
    r = ret.to_numpy()
    reb = rebalance.reindex(ret.index).fillna(False).to_numpy()
    fee = _per_market(fee_bps, mk) / 1e4
    spr = _per_market(half_spread_bps, mk) / 1e4
    if funding is None:
        fund = np.zeros_like(r)
    elif isinstance(funding, pd.DataFrame):
        fund = funding.reindex(index=ret.index, columns=mk).fillna(0.0).to_numpy()
    else:
        fund = np.tile(_per_market(funding, mk) / HOURS_PER_YEAR, (len(r), 1))

    n = len(ret)
    eq, gross = np.empty(n), np.empty(n)
    turn, fees, sprd, fpay = np.zeros(n), np.zeros(n), np.zeros(n), np.zeros(n)
    E = 1.0
    h = np.zeros(len(mk))  # dollar position per market
    for t in range(n):
        if t > 0 and E > 0:
            E += h @ r[t]                 # 1. price P&L
            h = h * (1 + r[t])
            f = h @ fund[t]               # 2. funding (+ = we pay)
            E -= f
            fpay[t] = f / (E + f)
        if reb[t] and E > 0:              # 3. trade to target
            trade = w[t] * E - h
            if band > 0:
                trade[np.abs(trade) < band * E] = 0.0
            c_fee = np.abs(trade) @ fee
            c_spr = np.abs(trade) @ spr
            E0 = E
            h = h + trade
            E -= c_fee + c_spr
            turn[t] = np.abs(trade).sum() / E0
            fees[t], sprd[t] = c_fee / E0, c_spr / E0
        eq[t] = E
        gross[t] = np.abs(h).sum() / E if E > 0 else np.nan
    s = lambda a: pd.Series(a, ret.index)
    return Result(s(eq), s(gross), s(turn), s(fees), s(sprd), s(fpay))


def metrics(res: Result, start=None, end=None) -> dict:
    """Headline numbers on daily (00:00 UTC) equity, like the competition."""
    eq = res.equity.loc[start:end]
    eq = eq / eq.iloc[0]
    daily = eq.resample("D").last()
    dr = daily.pct_change().dropna()
    days = len(dr)
    tot = daily.iloc[-1] - 1
    sd = dr.std()
    sl = slice(eq.index[0], eq.index[-1])
    return {
        "days": days,
        "total_ret_%": 100 * tot,
        "ann_ret_%": 100 * ((1 + tot) ** (365 / days) - 1),
        "daily_std_%": 100 * sd,
        "sharpe": dr.mean() / sd * np.sqrt(365) if sd > 0 else np.nan,
        "max_dd_%": 100 * (eq / eq.cummax() - 1).min(),
        "worst_day_%": 100 * dr.min(),
        "avg_gross": res.gross.loc[sl].mean(),
        "turnover_x_per_yr": res.turnover.loc[sl].sum() * 365 / days,
        "fees_%": 100 * res.fees.loc[sl].sum(),
        "spread_%": 100 * res.spread.loc[sl].sum(),
        "funding_%": 100 * res.funding.loc[sl].sum(),
    }


def rolling_windows(res: Result, days: int = 14, start=None, end=None) -> dict:
    """Distribution over every `days`-long window: a stand-in for an
    unknown-length competition starting on an unknown day."""
    daily = res.equity.loc[start:end].resample("D").last()
    dr = daily.pct_change()
    ret_w = daily.pct_change(days).dropna()
    sd_w = dr.rolling(days).std().dropna()
    return {
        f"{days}d_ret_med_%": 100 * ret_w.median(),
        f"{days}d_ret_p10_%": 100 * ret_w.quantile(0.1),
        f"{days}d_std_med_%": 100 * sd_w.median(),
        f"{days}d_pct_positive": 100 * (ret_w > 0).mean(),
    }


# Competition weights. The report (30%) can't be backtested, so scores
# below are out of 70.
W_RETURN, W_LOWVOL = 40.0, 30.0


def score(table: pd.DataFrame, ret_range=None, std_range=None) -> pd.Series:
    """Combined score (out of 70) for each row of a metrics table.

    The real score min-max normalizes each metric across all teams, which
    we can't see. Two stand-ins:
    - relative (default): normalize across the rows given, i.e. rank our
      strategies against each other. Only compare rows from one period.
    - absolute: pass ret_range=(lo, hi) in total-return % and
      std_range=(lo, hi) in daily-std %, our guess at the field's spread.
      Values outside the range are clipped.
    """
    ret, sd = table["total_ret_%"], table["daily_std_%"]
    rlo, rhi = ret_range or (ret.min(), ret.max())
    slo, shi = std_range or (sd.min(), sd.max())
    r_norm = ((ret - rlo) / (rhi - rlo or 1)).clip(0, 1)
    v_norm = ((shi - sd) / (shi - slo or 1)).clip(0, 1)   # lower std = better
    return W_RETURN * r_norm + W_LOWVOL * v_norm
