"""Load and clean the organizers' hourly candles (historical/*.csv).

Cleaning rules:
- Rows with trades == 0 are not real prices (the README says such hours
  should be absent, but EWJ/USDEUR/USDJPY/EWY contain them). Their OHLC is
  set to NaN.
- All markets are aligned on one hourly UTC index. Missing closes are
  forward-filled, so a stale market shows a 0 return rather than a fake one.
- `live` marks hours after a market's first real candle; strategies must
  hold 0 before that.
"""
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "historical"

MARKETS = ["BTC", "ETH", "HYPE", "SP500", "EWY", "EWJ",
           "BRENT", "GOLD", "USDEUR", "USDJPY"]
CRYPTO = ["BTC", "ETH", "HYPE"]
# No zero-trade hours and <1% of hours with <=5 trades.
CLEAN = ["BTC", "ETH", "HYPE", "SP500", "EWY", "BRENT", "GOLD"]
THIN = ["EWJ", "USDEUR", "USDJPY"]


def load_market(name: str) -> pd.DataFrame:
    df = pd.read_csv(HIST / f"{name}.csv")
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()
    dead = df["trades"] == 0
    df.loc[dead, ["open", "high", "low", "close"]] = np.nan
    return df


def load_panel(markets=MARKETS):
    """Returns (close, ret, live, raw).

    close: hourly closes, forward-filled, NaN before listing.
    ret:   simple close-to-close returns, 0 where not live.
    live:  bool, True once the market has a real price.
    raw:   dict of the cleaned per-market frames.
    """
    raw = {m: load_market(m) for m in markets}
    start = min(df.index[0] for df in raw.values())
    end = max(df.index[-1] for df in raw.values())
    idx = pd.date_range(start, end, freq="h", tz="UTC")
    close = pd.DataFrame({m: raw[m]["close"].reindex(idx) for m in markets})
    live = close.notna().cummax()
    close = close.ffill()
    ret = close.pct_change(fill_method=None).fillna(0.0).where(live, 0.0)
    return close, ret, live, raw
