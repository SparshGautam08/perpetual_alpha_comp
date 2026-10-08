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


class Drift:
    """Funding + drift strategy, LOCKED rules from REPORT_NOTES E13 (Oct 8).

    Every loop, per market: gap = (par mid / oracle - 1) in bp.
    - Flat and |gap| >= ENTRY_BP and funding pays our side: trade toward the
      oracle (gap > 0: sell $NOTIONAL; gap < 0: buy).
    - Holding: close when the gap is back within EXIT_BP (crossed to our
      side of 0), or after MAX_HOLD_H hours.
    Do not change these numbers without logging a new experiment."""
    name = "drift_v1"
    ENTRY_BP = 40.0
    EXIT_BP = 0.0
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
            gap = ((q.ask + q.bid) / 2 / q.oracle - 1) * 1e4
            held = positions.get(coin, 0.0)
            if held != 0:
                e = entries.get(coin)
                side = 1 if held > 0 else -1
                # We're short when the gap was positive: closed once gap <= EXIT_BP.
                closed = (side < 0 and gap <= self.EXIT_BP) or (side > 0 and gap >= -self.EXIT_BP)
                too_old = e is not None and now_s - e["t"] >= self.MAX_HOLD_H * 3600
                if closed or too_old:
                    orders[coin] = (0.0, f"exit ({'gap closed' if closed else 'max hold'}), gap {gap:+.1f} bp")
                continue
            if abs(gap) < self.ENTRY_BP:
                continue
            side = -1 if gap > 0 else 1              # mid above oracle -> sell
            if (q.funding > 0) != (side < 0):        # funding must pay our side
                continue
            orders[coin] = (side * self.NOTIONAL,
                            f"entry, gap {gap:+.1f} bp, funding {q.funding * 24 * 365 * 100:+.0f}%/yr")
        return orders

    def record(self, coin: str, now_s: float, opened: bool):
        entries = self.state.get("drift_entries", {})
        if opened:
            entries[coin] = {"t": now_s}
        else:
            entries.pop(coin, None)
        self.state.set(drift_entries=entries)
