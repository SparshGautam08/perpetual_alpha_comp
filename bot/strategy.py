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
