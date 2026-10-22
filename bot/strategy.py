"""Strategy v1: risk parity at a volatility target, the most consistent
baseline in the backtests (REPORT_NOTES E4-E5). Uses the same code as the
backtester so live and backtest weights match.

A strategy is anything with target_weights() -> {coin: weight}, where
weight = signed notional / equity. Swap in a better one later."""
import logging

import numpy as np
import pandas as pd

from backtest.strategies import ewma_cov, risk_parity

from . import config
from .market import mainnet_hourly_returns, post
from .mlsignal import latest_predictions

log = logging.getLogger("bot")


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
    # Catastrophe stop (E31): close if the trade loses this much at the oracle
    # vs the oracle at entry. Never triggered in a week of data (worst trade
    # ~-100 bp); insurance against rare crashes, not a performance lever.
    STOP_BP = 200.0
    NOTIONAL = 25_000.0
    # At most this many drift positions at once (backtest max: 4). Guards a new
    # venue where many markets could sit off the oracle at the same time.
    MAX_OPEN = int(__import__("os").environ.get("DRIFT_MAX_OPEN", 4))
    MARKETS = ["BTC", "ETH", "HYPE", "SP500", "EWY", "EWJ", "BRENT", "GOLD",
               "USDEUR", "USDJPY"]

    def __init__(self, state):
        self.state = state          # persists entry times across restarts

    def update_basis(self, quotes, now_s: float) -> dict[str, tuple[float, int]]:
        """E47: once a minute, record each market's book-mid premium vs the oracle
        (bp); return {coin: (median over the last DRIFT_BASIS_H hours, samples)}."""
        hist = {c: list(v) for c, v in self.state.get("drift_basis", {}).items()}
        minute, keep = int(now_s // 60), int(config.DRIFT_BASIS_H * 60)
        changed = False
        for coin in self.MARKETS:
            q = quotes.get(coin)
            if q is None or not q.ok():
                continue
            h = hist.setdefault(coin, [])
            if not h or h[-1][0] != minute:
                h.append([minute, round(((q.bid + q.ask) / 2 / q.oracle - 1) * 1e4, 2)])
                changed = True
            hist[coin] = [x for x in h if x[0] > minute - keep]
        if changed:
            self.state.set(drift_basis=hist)
        return {c: (float(np.median([x[1] for x in h])), len(h)) for c, h in hist.items() if h}

    def tick(self, quotes, positions: dict[str, float], now_s: float) -> dict[str, tuple]:
        """Returns {coin: (target_usd, reason)} for markets that should trade now."""
        entries = self.state.get("drift_entries", {})
        orders = {}
        basis = self.update_basis(quotes, now_s) if config.DRIFT_BASIS else {}
        # ML Model 1's predicted 15 s oracle move (E39), read from shadow mode.
        # Always logged with entries (for the live check); used only if ML_FILTER.
        preds = latest_predictions(now_s)
        candidates = []          # (edge_bp, coin, order): entries, best first if capped
        for coin in self.MARKETS:
            q = quotes.get(coin)
            if q is None or not q.ok():
                continue
            held = positions.get(coin, 0.0)
            # Reference price: the oracle, or (E47) the oracle shifted by the
            # market's usual premium, so a standing discount is not a "gap".
            b, nb = basis.get(coin, (0.0, 0))
            warm = nb >= config.DRIFT_BASIS_MIN
            ref = q.oracle * (1 + (b if config.DRIFT_BASIS and (warm or held) and nb else 0.0) / 1e4)
            if held != 0:
                usd = abs(held) * q.oracle
                # Short closes by buying, long by selling; cost vs oracle in bp.
                px = fill_price(q.asks if held < 0 else q.bids, usd)
                if px is None:
                    continue
                cost = (px / ref - 1) * 1e4 * (1 if held < 0 else -1)
                e = entries.get(coin)
                too_old = e is not None and now_s - e["t"] >= self.MAX_HOLD_H * 3600
                stopped = (e is not None and e.get("o") and
                           (1 if held > 0 else -1) * (q.oracle / e["o"] - 1) * 1e4 <= -self.STOP_BP)
                if cost <= self.EXIT_COST_BP or too_old or stopped:
                    why = ("cheap exit" if cost <= self.EXIT_COST_BP else
                           "STOP" if stopped else "max hold")
                    orders[coin] = (0.0, f"exit ({why}), close cost {cost:+.1f} bp")
                continue
            if config.DRIFT_BASIS and not warm:
                continue                 # usual premium not known yet (restart warm-up)
            sell = fill_price(q.bids, self.NOTIONAL)
            buy = fill_price(q.asks, self.NOTIONAL)
            sell_edge = (sell / ref - 1) * 1e4 if sell else -1e9
            buy_edge = (1 - buy / ref) * 1e4 if buy else -1e9
            vs = f"usual level ({b:+.1f} bp vs oracle)" if config.DRIFT_BASIS else "oracle"
            p = preds.get(coin)
            # E39 rule: a predicted oracle rise eats a short's edge and adds to a long's.
            adj = p if (config.ML_FILTER and p is not None) else 0.0
            ml = (f", ML pred {p:+.1f} bp" + (" (filter on)" if config.ML_FILTER else "")
                  if p is not None else ", ML pred n/a")
            if sell_edge - adj >= self.ENTRY_EDGE_BP:
                candidates.append((sell_edge - adj, coin, (-self.NOTIONAL, f"entry: sell ${self.NOTIONAL:,.0f} fills "
                                   f"{sell_edge:+.1f} bp vs {vs}, funding {q.funding * 8760 * 100:+.0f}%/yr{ml}")))
            elif buy_edge + adj >= self.ENTRY_EDGE_BP:
                candidates.append((buy_edge + adj, coin, (self.NOTIONAL, f"entry: buy ${self.NOTIONAL:,.0f} fills "
                                   f"{buy_edge:+.1f} bp under {vs}, funding {q.funding * 8760 * 100:+.0f}%/yr{ml}")))
            elif config.ML_FILTER and p is not None and max(sell_edge, buy_edge) >= self.ENTRY_EDGE_BP:
                log.info(f"ML filter: skip {coin} entry (sell {sell_edge:+.1f} / buy {buy_edge:+.1f} bp, "
                         f"ML pred {p:+.1f} bp)")
        slots = max(0, self.MAX_OPEN - sum(1 for u in positions.values() if u))
        candidates.sort(reverse=True)
        for _, coin, order in candidates[:slots]:
            orders[coin] = order
        if len(candidates) > slots:
            log.info(f"Drift cap ({self.MAX_OPEN} open): skipped entries "
                     + ", ".join(f"{c} {e:+.1f} bp" for e, c, _ in candidates[slots:]))
        return orders

    def record(self, coin: str, now_s: float, opened: bool, oracle: float | None = None):
        entries = self.state.get("drift_entries", {})
        if opened:
            entries[coin] = {"t": now_s, "o": oracle}
        else:
            entries.pop(coin, None)
        self.state.set(drift_entries=entries)


class Carry:
    """Funding carry, LOCKED rule from REPORT_NOTES E46 (Oct 21).

    On pas, funding has been one-sided for days (longs paid ~100%/yr on
    SP500). Hold CARRY_COIN on the side that receives funding:
    - signal = mean of the last CARRY_LOOKBACK_H settled hourly funding
      rates, annualized in % (+ = longs pay);
    - flat: go long if signal <= -ENTER, short if signal >= +ENTER;
    - long: exit if signal > -EXIT; short: exit if signal < +EXIT.
    The size is fixed at entry (CARRY_USD at the oracle) so there is no
    churn; checked once an hour. Do not change without logging first."""
    name = "carry_v1"
    MIN_HOURS = 12           # fewer settled rates than this: keep the current side

    def __init__(self, state):
        self.state = state

    @staticmethod
    def signal(now_s: float) -> tuple[float | None, int]:
        start = int((now_s - config.CARRY_LOOKBACK_H * 3600) * 1000)
        h = post(config.BASE_URL, {"type": "fundingHistory",
                                   "coin": f"{config.DEX}:{config.CARRY_COIN}", "startTime": start})
        rates = [float(x["fundingRate"]) for x in h][-config.CARRY_LOOKBACK_H:]
        if not rates:
            return None, 0
        return sum(rates) / len(rates) * 8760 * 100, len(rates)

    @staticmethod
    def decide(side: int, sig: float | None, n: int) -> int:
        """New side (+1 long, -1 short, 0 flat) from the current side and signal."""
        if sig is None or n < Carry.MIN_HOURS:
            return side
        enter, exit_ = config.CARRY_ENTER_PCT, config.CARRY_EXIT_PCT
        if side == 0:
            return 1 if sig <= -enter else (-1 if sig >= enter else 0)
        if side > 0:
            return 0 if sig > -exit_ else 1
        return 0 if sig < exit_ else -1

    def update(self, quotes, now_s: float) -> tuple[dict, bool]:
        """Hourly: returns ({coin: target units}, changed?)."""
        units = dict(self.state.get("carry_target_units", {}))
        hour = int(now_s // 3600)
        if config.CARRY_USD <= 0 or self.state.get("carry_hour") == hour:
            return units, False
        q = quotes.get(config.CARRY_COIN)
        if q is None:
            return units, False
        sig, n = self.signal(now_s)
        side = int(self.state.get("carry_side", 0))
        new = self.decide(side, sig, n)
        self.state.set(carry_hour=hour)
        log.info(f"CARRY {config.CARRY_COIN}: funding last {n} h {sig if sig is None else round(sig, 1)} %/yr, "
                 f"side {side:+d} -> {new:+d}")
        if new == side:
            return units, False
        units = {config.CARRY_COIN: new * config.CARRY_USD / q.oracle} if new else {}
        self.state.set(carry_side=new, carry_target_units=units)
        return units, True
