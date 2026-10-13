"""Inputs ("features") for ML Model 1, the par oracle forecast (E23).

ONE function used by both training (backtest/ml_forecast.py) and live
prediction (bot/shadow.py), so live inputs are computed exactly like the
inputs the model was trained and tested on.

Input: a 1-second grid (UTC DatetimeIndex, last value at or before each
second, forward-filled) for one market with columns
  o    par oracle price          pmid  par book mid (activeAssetCtx midPx)
  m    mainnet best bid/ask mid  mo    mainnet oracle price
Output: one row per second with the columns in FEATURES. The first ~300
rows are NaN while rolling windows fill up.
"""
import numpy as np
import pandas as pd

# par market -> mainnet market. ORDER MATTERS: "market" feature = position here.
PAIRS = {"BTC": "BTC", "ETH": "ETH", "HYPE": "HYPE", "SP500": "xyz:SP500",
         "EWY": "xyz:EWY", "EWJ": "xyz:EWJ", "BRENT": "xyz:BRENTOIL",
         "GOLD": "xyz:GOLD", "USDEUR": "xyz:EUR", "USDJPY": "xyz:JPY"}
MARKET_ID = {c: k for k, c in enumerate(PAIRS)}

FEATURES = ["prem_raw", "prem_dm", "prem_dm_l1", "prem_dm_l3", "prem_dm_l5", "prem_dm_l10",
            "m_ret_1", "m_ret_5", "m_ret_15", "m_ret_60", "m_vol_300",
            "o_ret_5", "o_ret_15", "o_ret_60", "o_stale_s", "parmid_vs_oracle",
            "moracle_vs_oracle", "hour", "us_open", "market"]
WARMUP_S = 1800       # history needed for the full rolling median (min 300 s)


def compute_features(g: pd.DataFrame, coin: str) -> pd.DataFrame:
    lo, lm = np.log(g.o), np.log(g.m)
    f = pd.DataFrame(index=g.index)
    f["prem_raw"] = (lm - lo) * 1e4
    f["prem_dm"] = f.prem_raw - f.prem_raw.rolling(1800, min_periods=300).median()
    for L in (1, 3, 5, 10):
        f[f"prem_dm_l{L}"] = f.prem_dm.shift(L)
    for L in (1, 5, 15, 60):
        f[f"m_ret_{L}"] = lm.diff(L) * 1e4
    f["m_vol_300"] = lm.diff().mul(1e4).rolling(300).std()
    for L in (5, 15, 60):
        f[f"o_ret_{L}"] = lo.diff(L) * 1e4
    changed = lo.diff().ne(0)
    f["o_stale_s"] = changed.groupby(changed.cumsum()).cumcount()
    f["parmid_vs_oracle"] = (np.log(g.pmid) - lo) * 1e4
    f["moracle_vs_oracle"] = (np.log(g.mo) - lo) * 1e4
    f["hour"] = g.index.hour
    mins = g.index.hour * 60 + g.index.minute
    f["us_open"] = ((g.index.dayofweek < 5) & (mins >= 13 * 60 + 30) & (mins < 20 * 60)).astype(int)
    f["market"] = MARKET_ID[coin]
    return f
