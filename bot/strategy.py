"""Strategy v1: risk parity at a volatility target, the most consistent
baseline in the backtests (REPORT_NOTES E4-E5). Uses the same code as the
backtester so live and backtest weights match.

A strategy is anything with target_weights() -> {coin: weight}, where
weight = signed notional / equity. Swap in a better one later."""
import numpy as np
import pandas as pd

from backtest.strategies import ewma_cov, risk_parity

from . import config
from .market import mainnet_hourly_returns


class RiskParity:
    name = "risk_parity_v1"

    def target_weights(self) -> dict[str, float]:
        ret = mainnet_hourly_returns(config.MARKETS, config.HISTORY_DAYS)
        live = pd.DataFrame(True, index=ret.index, columns=ret.columns)
        cov = ewma_cov(ret, halflife=24 * 7)
        w = risk_parity(ret, live, target_vol=config.VOL_TARGET, cov=cov,
                        max_gross=config.MAX_GROSS).iloc[-1] * config.LEVERAGE
        return apply_caps(w.to_dict())


def apply_caps(w: dict[str, float]) -> dict[str, float]:
    """Per-market cap, then total gross cap."""
    w = {k: float(np.clip(v, -config.MAX_WEIGHT, config.MAX_WEIGHT)) for k, v in w.items()}
    gross = sum(abs(v) for v in w.values())
    if gross > config.MAX_GROSS:
        w = {k: v * config.MAX_GROSS / gross for k, v in w.items()}
    return w


def fill_price(levels, usd: float):
    """Average price to fill $usd against book levels [(px, sz), ...],
    best first. None if the book can't fill it."""
    left, units = usd, 0.0
    for px, sz in levels or []:
        take = min(left, px * sz)
        units += take / px
        left -= take
        if left <= 1e-6:
            return usd / units
    return None


class Drift:
    """Funding + drift strategy v2, LOCKED rules from REPORT_NOTES E16 (Oct 9).
    v1 (E13) measured the gap at the top-of-book mid and failed out of sample
    (E15): small orders from other traders distort the mid.

    Every loop, per market, using the price we can actually trade $NOTIONAL at:
    - Flat: if selling $NOTIONAL fills >= ENTRY_EDGE_BP above the oracle,
      sell; if buying fills >= ENTRY_EDGE_BP below it, buy.
    - Holding: close when closing would cost <= EXIT_COST_BP vs the oracle,
      or after MAX_HOLD_H hours.
    Do not change these numbers without logging a new experiment."""
    name = "drift_v2"
    # 40 bp = v2 (E16); 30 bp = v2b, which passed its locked test (E27).
    ENTRY_EDGE_BP = float(__import__("os").environ.get("DRIFT_ENTRY_BP", 40.0))
    EXIT_COST_BP = 10.0
    MAX_HOLD_H = 12.0
    NOTIONAL = 25_000.0
    MARKETS = ["BTC", "ETH", "HYPE", "SP500", "EWY", "EWJ", "BRENT", "GOLD",
               "USDEUR", "USDJPY"]

    def __init__(self, state):
        self.state = state          # persists entry times across restarts

    def tick(self, quotes, positions: dict[str, float], now_s: float) -> dict[str, tuple]:
        """Returns {coin: (target_usd, reason)} for markets that should trade now."""
        entries = self.state.get("drift_entries", {})
        orders = {}
        for coin in self.MARKETS:
            q = quotes.get(coin)
            if q is None or not q.ok():
                continue
            held = positions.get(coin, 0.0)
            if held != 0:
                usd = abs(held) * q.oracle
                # Short closes by buying, long by selling; cost vs oracle in bp.
                px = fill_price(q.asks if held < 0 else q.bids, usd)
                if px is None:
                    continue
                cost = (px / q.oracle - 1) * 1e4 * (1 if held < 0 else -1)
                e = entries.get(coin)
                too_old = e is not None and now_s - e["t"] >= self.MAX_HOLD_H * 3600
                if cost <= self.EXIT_COST_BP or too_old:
                    why = "cheap exit" if cost <= self.EXIT_COST_BP else "max hold"
                    orders[coin] = (0.0, f"exit ({why}), close cost {cost:+.1f} bp")
                continue
            sell = fill_price(q.bids, self.NOTIONAL)
            buy = fill_price(q.asks, self.NOTIONAL)
            sell_edge = (sell / q.oracle - 1) * 1e4 if sell else -1e9
            buy_edge = (1 - buy / q.oracle) * 1e4 if buy else -1e9
            if sell_edge >= self.ENTRY_EDGE_BP:
                orders[coin] = (-self.NOTIONAL, f"entry: sell ${self.NOTIONAL:,.0f} fills "
                                f"{sell_edge:+.1f} bp vs oracle, funding {q.funding * 8760 * 100:+.0f}%/yr")
            elif buy_edge >= self.ENTRY_EDGE_BP:
                orders[coin] = (self.NOTIONAL, f"entry: buy ${self.NOTIONAL:,.0f} fills "
                                f"{buy_edge:+.1f} bp under oracle, funding {q.funding * 8760 * 100:+.0f}%/yr")
        return orders

    def record(self, coin: str, now_s: float, opened: bool):
        entries = self.state.get("drift_entries", {})
        if opened:
            entries[coin] = {"t": now_s}
        else:
            entries.pop(coin, None)
        self.state.set(drift_entries=entries)
