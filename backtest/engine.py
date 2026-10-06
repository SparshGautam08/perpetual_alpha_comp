"""Hourly portfolio simulator.

Convention (no lookahead): weights[t] are decided with data up to and
including the close of hour t, and are held over hour t+1. Weights are
signed notional / equity, so sum(|w|) is gross leverage.

Between rebalances, positions drift with prices. At each rebalance hour
we trade to target and pay (fee_bps + slip_bps) on traded notional.
Funding is NOT modelled (not in the historical data).
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Result:
    equity: pd.Series      # hourly equity, starts at 1.0
    gross: pd.Series       # gross leverage after rebalance
    turnover: pd.Series    # traded notional / equity per hour
    fees: pd.Series        # fees as fraction of equity per hour


def daily_schedule(index: pd.DatetimeIndex, hour: int = 0) -> pd.Series:
    return pd.Series(index.hour == hour, index=index)


def backtest(weights: pd.DataFrame, ret: pd.DataFrame, rebalance: pd.Series,
             fee_bps: float = 5.0, slip_bps: float = 2.0,
             band: float = 0.0) -> Result:
    """band: skip a market's trade if |target - current| < band * equity."""
    w = weights.reindex(ret.index).fillna(0.0).to_numpy()
    r = ret.to_numpy()
    reb = rebalance.reindex(ret.index).fillna(False).to_numpy()
    cost = (fee_bps + slip_bps) / 1e4
    n = len(ret)
    eq = np.empty(n); gross = np.empty(n); turn = np.zeros(n); fees = np.zeros(n)
    E = 1.0
    h = np.zeros(r.shape[1])  # dollar positions
    for t in range(n):
        if t > 0:
            pnl = h @ r[t]
            h = h * (1 + r[t])
            E += pnl
        if reb[t] and E > 0:
            target = w[t] * E
            trade = target - h
            if band > 0:
                trade[np.abs(trade) < band * E] = 0.0
            c = cost * np.abs(trade).sum()
            h = h + trade
            E -= c
            turn[t] = np.abs(trade).sum() / (E + c)
            fees[t] = c / (E + c)
        eq[t] = E
        gross[t] = np.abs(h).sum() / E if E > 0 else np.nan
    idx = ret.index
    return Result(pd.Series(eq, idx), pd.Series(gross, idx),
                  pd.Series(turn, idx), pd.Series(fees, idx))


def metrics(res: Result, start=None, end=None) -> dict:
    """Scored on daily (00:00 UTC) equity, like the competition."""
    eq = res.equity.loc[start:end]
    eq = eq / eq.iloc[0]
    daily = eq.resample("D").last()
    dr = daily.pct_change().dropna()
    days = len(dr)
    tot = daily.iloc[-1] - 1
    sd = dr.std()
    dd = (eq / eq.cummax() - 1).min()
    sl = slice(eq.index[0], eq.index[-1])
    return {
        "days": days,
        "total_ret_%": 100 * tot,
        "ann_ret_%": 100 * ((1 + tot) ** (365 / days) - 1),
        "daily_std_%": 100 * sd,
        "sharpe": dr.mean() / sd * np.sqrt(365) if sd > 0 else np.nan,
        "max_dd_%": 100 * dd,
        "worst_day_%": 100 * dr.min(),
        "avg_gross": res.gross.loc[sl].mean(),
        "turnover_x_per_yr": res.turnover.loc[sl].sum() * 365 / days,
        "fees_%": 100 * res.fees.loc[sl].sum(),
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
