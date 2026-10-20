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
import json
import logging
import time

import numpy as np

from . import config
from .alerts import alert
from .execution import close, _trade
from .market import snapshot
from .strategy import Carry, Drift, RiskParity

log = logging.getLogger("bot")


class Combined:
    name = "combined_rp_drift_v2"

    def __init__(self, state):
        self.state = state
        self.rp = RiskParity()
        self.drift = Drift(state)
        self.carry = Carry(state)
        self.markets = sorted(set(config.MARKETS) | set(Drift.MARKETS))

    def reset(self):
        """After a flatten (kill switch / loss stop): no targets, no shares."""
        self.state.set(rp_target_units={}, drift_units={}, drift_entries={},
                       carry_target_units={}, carry_side=0)

    def step(self, broker, quotes, equity, rebalance_due: bool) -> dict:
        rp_t = dict(self.state.get("rp_target_units", {}))
        drift_now = {c: u for c, u in self.state.get("drift_units", {}).items() if u}
        if rebalance_due:
            w = self.rp.target_weights()
            rp_t = {c: w[c] * equity / quotes[c].oracle for c in w if c in quotes}
            log.info("RP targets ($): " + ", ".join(f"{c} {u * quotes[c].oracle:,.0f}"
                                                   for c, u in sorted(rp_t.items())))
            self.state.set(rp_target_units=rp_t)
        # Funding carry (E46) shares the non-drift slot with risk parity (off):
        # its target is held every loop, so a partial fill is completed later.
        carry_t, carry_changed = self.carry.update(quotes, time.time())
        if carry_changed:
            alert("carry", f"Carry target now {carry_t or 'flat'} (side {self.state.get('carry_side')})",
                  priority="high")
        base_t = {c: rp_t.get(c, 0.0) + carry_t.get(c, 0.0) for c in set(rp_t) | set(carry_t)}

        # Dust: a drift share too small to trade (< exchange minimum) is just
        # forgotten (it becomes part of risk parity's share), otherwise the
        # exit would be retried every loop forever (seen Oct 10: -0.02 HYPE).
        dust = [c for c, u in drift_now.items()
                if c in quotes and abs(u) * quotes[c].oracle < config.MIN_ORDER_USD]
        if dust:
            log.info(f"Drift dust dropped: {dict((c, drift_now[c]) for c in dust)}")
            for c in dust:
                drift_now.pop(c)
                self.drift.record(c, time.time(), opened=False)
            self.state.set(drift_units=drift_now)

        orders = self.drift.tick(quotes, drift_now, time.time())
        drift_t = dict(drift_now)
        for c, (usd, reason) in orders.items():
            drift_t[c] = usd / quotes[c].oracle if usd else 0.0
            log.info(f"DRIFT {c}: {reason}")

        actual = broker.positions()
        # The maker (separate process, own key) owns part of some positions; it
        # writes its own inventory to maker_units_live.json. Exclude it so risk
        # parity never trades the maker's inventory as its own.
        maker = self._maker_units(broker)
        for c, u in maker.items():
            actual[c] = actual.get(c, 0.0) - u
        fills_all = []
        for c in sorted(set(base_t) | set(drift_t) | set(actual)):
            q = quotes.get(c)
            if q is None or not q.ok():
                continue
            cur = float(actual.get(c, 0.0))
            desired = float(base_t.get(c, 0.0) + drift_t.get(c, 0.0))
            delta = desired - cur
            d = float(drift_t.get(c, 0.0) - drift_now.get(c, 0.0))  # drift's wanted change
            r = delta - d                                             # the rest is risk parity's
            drift_trades = c in orders
            held_target = rebalance_due or (config.CARRY_USD > 0 and c == config.CARRY_COIN)
            rp_trades = held_target and abs(r) * q.oracle >= config.BAND * equity
            if not (drift_trades or rp_trades):
                continue
            if not rp_trades:          # drift-only loop: don't drag RP's small drift along
                r, delta = 0.0, d
            # What actually filled is measured from the exchange position
            # before vs after, not from order replies: if the trade raises
            # half-way (network error after a fill), drift's records still
            # match reality and the trade is never repeated (Oct 12 incident:
            # a crash right after an exit made the next loop exit again,
            # leaving an orphan 9.8 ETH long).
            err, fills = None, []
            try:
                fills = self._trade_units(broker, c, delta, cur, q)
                fills_all += fills
            except Exception as e:      # recorded below, then re-raised
                err = e
            if err is None:
                # Normal case: what OUR orders filled (a maker fill in the same
                # second can't be mistaken for drift's).
                f = sum(x["filled"] * (1 if x["side"] == "buy" else -1) for x in fills)
            else:
                # Error mid-trade: order replies may be lost, so measure the real
                # position change instead (E26).
                f = self._position_change(broker, c, cur + maker.get(c, 0.0))
            self._attribute(c, d, r, f, drift_now, q.sz_decimals, q.oracle)
            drift_now = {k: v for k, v in self.state.get("drift_units", {}).items() if v}
            if err is not None:
                log.error(f"{c}: trade failed after {f:+g} units filled; drift records updated")
                raise err
        return {"fills": fills_all}

    @staticmethod
    def _maker_units(broker) -> dict:
        if broker.mode != "live":
            return {}
        p = config.STATE_DIR / "maker_units_live.json"
        try:
            d = json.loads(p.read_text())
            return {c: float(u) for c, u in d.get("units", {}).items()}
        except FileNotFoundError:
            return {}
        except Exception as e:
            log.warning(f"maker_units_live.json unreadable: {e}")
            return {}

    @staticmethod
    def _position_change(broker, coin, before, tries=3):
        """Actual position change since `before`, read from the exchange
        (retried, since this runs right after an error)."""
        for i in range(tries):
            try:
                return float(broker.positions().get(coin, 0.0)) - before
            except Exception:
                if i == tries - 1:
                    raise
                time.sleep(2)

    @staticmethod
    def _trade_units(broker, coin, delta_units, cur_units, q):
        if abs(delta_units) * q.oracle < config.MIN_ORDER_USD:
            return []
        if abs(cur_units + delta_units) < 10 ** -q.sz_decimals:   # going flat
            return close(broker, coin, cur_units, q.oracle)
        # Size in units (not $ at the oracle) so drift's exits sell exactly what
        # its entries bought; $ sizing left ~1% behind on each exit (Oct 10).
        return _trade(broker, coin, delta_units * q.oracle, cur_units * q.oracle,
                      close_units=abs(delta_units))

    def _attribute(self, c, d, r, f, drift_now, dec, oracle=None):
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
        new = float(round(float(new), dec))     # plain float: numpy types break order signing
        units = dict(self.state.get("drift_units", {}))
        units[c] = new
        self.state.set(drift_units={k: v for k, v in units.items() if v})
        if old == 0 and new != 0:
            self.drift.record(c, time.time(), opened=True, oracle=oracle)
        elif old != 0 and new == 0:
            self.drift.record(c, time.time(), opened=False)
