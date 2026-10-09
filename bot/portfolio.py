"""Combined strategy: risk parity base + drift v2 in one account ("sleeves").

Each market's position = risk parity's share + drift's share.
- Drift's share is tracked exactly in state ("drift_units"): what its own
  trades opened and closed. Its exits only ever look at its own share.
- Risk parity's share is everything else (actual position - drift share).
  Its targets (in units) are set once a day at the rebalance and held.
- Every loop, per market: desired = rp target + drift target, and only the
  difference to the actual position is traded. Opposite wishes net out
  internally (fewer fees).
"""
import logging
import time

import numpy as np

from . import config
from .execution import close, _trade
from .market import snapshot
from .strategy import Drift, RiskParity

log = logging.getLogger("bot")


class Combined:
    name = "combined_rp_drift_v2"

    def __init__(self, state):
        self.state = state
        self.rp = RiskParity()
        self.drift = Drift(state)
        self.markets = sorted(set(config.MARKETS) | set(Drift.MARKETS))

    def reset(self):
        """After a flatten (kill switch / loss stop): no targets, no shares."""
        self.state.set(rp_target_units={}, drift_units={}, drift_entries={})

    def step(self, broker, quotes, equity, rebalance_due: bool) -> dict:
        rp_t = dict(self.state.get("rp_target_units", {}))
        drift_now = {c: u for c, u in self.state.get("drift_units", {}).items() if u}
        if rebalance_due:
            w = self.rp.target_weights()
            rp_t = {c: w[c] * equity / quotes[c].oracle for c in w if c in quotes}
            log.info("RP targets ($): " + ", ".join(f"{c} {u * quotes[c].oracle:,.0f}"
                                                   for c, u in sorted(rp_t.items())))
            self.state.set(rp_target_units=rp_t)

        orders = self.drift.tick(quotes, drift_now, time.time())
        drift_t = dict(drift_now)
        for c, (usd, reason) in orders.items():
            drift_t[c] = usd / quotes[c].oracle if usd else 0.0
            log.info(f"DRIFT {c}: {reason}")

        actual = broker.positions()
        fills_all = []
        for c in sorted(set(rp_t) | set(drift_t) | set(actual)):
            q = quotes.get(c)
            if q is None or not q.ok():
                continue
            cur = actual.get(c, 0.0)
            desired = rp_t.get(c, 0.0) + drift_t.get(c, 0.0)
            delta = desired - cur
            d = drift_t.get(c, 0.0) - drift_now.get(c, 0.0)     # drift's wanted change
            r = delta - d                                         # the rest is risk parity's
            drift_trades = c in orders
            rp_trades = rebalance_due and abs(r) * q.oracle >= config.BAND * equity
            if not (drift_trades or rp_trades):
                continue
            if not rp_trades:          # drift-only loop: don't drag RP's small drift along
                r, delta = 0.0, d
            fills = self._trade_units(broker, c, delta, cur, q)
            fills_all += fills
            f = sum(x["filled"] * (1 if x["side"] == "buy" else -1) for x in fills)
            self._attribute(c, d, r, f, drift_now, q.sz_decimals)
        return {"fills": fills_all}

    @staticmethod
    def _trade_units(broker, coin, delta_units, cur_units, q):
        if abs(delta_units) * q.oracle < config.MIN_ORDER_USD:
            return []
        if abs(cur_units + delta_units) < 10 ** -q.sz_decimals:   # going flat
            return close(broker, coin, cur_units, q.oracle)
        return _trade(broker, coin, delta_units * q.oracle, cur_units * q.oracle)

    def _attribute(self, c, d, r, f, drift_now, dec):
        """Decide how much of the filled amount f belongs to drift's share."""
        old = drift_now.get(c, 0.0)
        if d == 0:
            new = old
        elif r == 0 or np.sign(r) == np.sign(d):        # same direction: share the fill
            got = f * abs(d) / (abs(d) + abs(r)) if f else 0.0
            new = old + (np.sign(d) * min(abs(got), abs(d)))
        else:                                             # opposite: match internally first
            internal = min(abs(d), abs(r))
            extra = max(abs(d) - internal, 0.0)
            new = old + np.sign(d) * (internal + min(abs(f), extra))
        new = round(new, dec)
        units = dict(self.state.get("drift_units", {}))
        units[c] = new
        self.state.set(drift_units={k: v for k, v in units.items() if v})
        if old == 0 and new != 0:
            self.drift.record(c, time.time(), opened=True)
        elif old != 0 and new == 0:
            self.drift.record(c, time.time(), opened=False)
