"""Baseline strategies. Each returns target weights (signed notional /
equity) per hour, using only data up to that hour's close."""
import numpy as np
import pandas as pd

HOURS_PER_YEAR = 24 * 365


def equal_weight(ret: pd.DataFrame, live: pd.DataFrame, gross: float = 1.0):
    n_live = live.sum(axis=1).replace(0, np.nan)
    return live.astype(float).div(n_live, axis=0).fillna(0.0) * gross


def ewma_cov(ret: pd.DataFrame, halflife: float):
    """Per-hour EWMA covariance, returned as a 3-D numpy array [t, i, j]."""
    lam = 0.5 ** (1 / halflife)
    r = ret.to_numpy()
    n, k = r.shape
    out = np.empty((n, k, k))
    S = np.zeros((k, k)); wsum = 0.0
    for t in range(n):
        S = lam * S + np.outer(r[t], r[t])
        wsum = lam * wsum + 1.0
        out[t] = S / wsum
    return out


def _scale_to_target(raw: pd.DataFrame, cov: np.ndarray, target_vol: float,
                     max_gross: float) -> pd.DataFrame:
    w = raw.to_numpy().copy()
    for t in range(len(w)):
        v = w[t] @ cov[t] @ w[t]
        if v <= 0:
            w[t] = 0.0
            continue
        w[t] *= target_vol / np.sqrt(v * HOURS_PER_YEAR)
        g = np.abs(w[t]).sum()
        if g > max_gross:
            w[t] *= max_gross / g
    return pd.DataFrame(w, raw.index, raw.columns)


def risk_parity(ret, live, target_vol=0.10, halflife=24 * 7, warmup=24 * 7,
                max_gross=3.0, cov=None):
    """Long-only inverse-vol weights, scaled so EWMA portfolio vol hits
    target_vol (annualized)."""
    cov = ewma_cov(ret, halflife) if cov is None else cov
    var = np.einsum("tii->ti", cov)
    sig = pd.DataFrame(np.sqrt(var), ret.index, ret.columns)
    ok = live & (live.cumsum() > warmup)
    raw = (1.0 / sig).where(ok & (sig > 0), 0.0)
    return _scale_to_target(raw, cov, target_vol, max_gross)


def trend(ret, live, lookbacks=(24 * 7, 24 * 30), target_vol=0.10,
          halflife=24 * 7, max_gross=3.0, long_only=False, cov=None):
    """Time-series momentum: per market, average of sign(trailing log
    return) over the lookbacks, sized by inverse vol, then the portfolio
    is scaled to target_vol."""
    cov = ewma_cov(ret, halflife) if cov is None else cov
    sig = pd.DataFrame(np.sqrt(np.einsum("tii->ti", cov)), ret.index, ret.columns)
    lr = np.log1p(ret)
    s = sum(np.sign(lr.rolling(L).sum()) for L in lookbacks) / len(lookbacks)
    if long_only:
        s = s.clip(lower=0)
    ok = live & (live.cumsum() > max(lookbacks))
    raw = (s / sig).where(ok & (sig > 0), 0.0).fillna(0.0)
    return _scale_to_target(raw, cov, target_vol, max_gross)
