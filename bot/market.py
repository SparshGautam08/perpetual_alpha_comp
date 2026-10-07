"""Read-only market data: par snapshot (oracle, mark, funding, best quotes)
and mainnet hourly candles for volatility estimates."""
import time
from dataclasses import dataclass

import pandas as pd
import requests

from . import config

_session = requests.Session()


def post(url: str, payload: dict):
    r = _session.post(url.rstrip("/") + "/info", json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


@dataclass
class Quote:
    coin: str            # short name, e.g. "BTC"
    name: str            # exchange name, e.g. "par:BTC"
    sz_decimals: int
    oracle: float
    mark: float
    funding: float       # hourly rate, + = longs pay
    bid: float
    bid_usd: float
    ask: float
    ask_usd: float
    bids: list = None    # [(px, sz), ...] best first
    asks: list = None

    @property
    def spread_bp(self) -> float:
        return (self.ask / self.bid - 1) * 1e4

    def ok(self) -> bool:
        """Sanity check before trading on this quote."""
        return (self.oracle > 0 and self.bid > 0 and self.ask > self.bid
                and self.spread_bp < config.MAX_SPREAD_BP)


def snapshot(coins: list[str]) -> dict[str, Quote]:
    meta, ctxs = post(config.BASE_URL, {"type": "metaAndAssetCtxs", "dex": config.DEX})
    by_name = {a["name"]: (a, c) for a, c in zip(meta["universe"], ctxs)}
    out = {}
    for coin in coins:
        name = f"{config.DEX}:{coin}"
        a, c = by_name[name]
        book = post(config.BASE_URL, {"type": "l2Book", "coin": name})["levels"]
        bid = book[0][0] if book[0] else {"px": 0, "sz": 0}
        ask = book[1][0] if book[1] else {"px": 0, "sz": 0}
        lv = lambda side: [(float(x["px"]), float(x["sz"])) for x in side]
        out[coin] = Quote(coin, name, a["szDecimals"], float(c["oraclePx"]),
                          float(c["markPx"]), float(c["funding"]),
                          float(bid["px"]), float(bid["px"]) * float(bid["sz"]),
                          float(ask["px"]), float(ask["px"]) * float(ask["sz"]),
                          lv(book[0]), lv(book[1]))
    return out


def mainnet_hourly_returns(coins: list[str], days: int) -> pd.DataFrame:
    """Hourly close-to-close returns from Hyperliquid mainnet, one column per
    par market. Only completed hours are used."""
    now = int(time.time() * 1000)
    start = now - days * 86_400_000
    closes = {}
    for coin in coins:
        c = post(config.MAINNET_URL, {"type": "candleSnapshot", "req": {
            "coin": config.MAINNET_COIN[coin], "interval": "1h",
            "startTime": start, "endTime": now}})
        s = pd.Series({pd.Timestamp(x["t"], unit="ms", tz="UTC"): float(x["c"]) for x in c})
        closes[coin] = s.iloc[:-1]           # drop the still-forming hour
    px = pd.DataFrame(closes).sort_index().ffill()
    return px.pct_change().dropna(how="all").fillna(0.0)
