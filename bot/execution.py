"""Turn target weights into orders: compare with current positions, skip
tiny changes, slice big ones, and send price-capped IoC orders."""
import logging
import time

from . import config
from .broker import round_px
from .market import Quote, snapshot

log = logging.getLogger("bot")
MAX_SLICES = 20     # per market per rebalance


def rebalance(broker, targets: dict[str, float], quotes: dict[str, Quote],
              equity: float) -> list[dict]:
    """Trade every market toward its target weight. Returns a list of fills."""
    fills = []
    pos = broker.positions()
    for coin in sorted(set(targets) | set(pos)):
        q = quotes.get(coin)
        if q is None or not q.ok():
            log.warning(f"{coin}: skipping, bad quote {q}")
            continue
        cur_usd = pos.get(coin, 0.0) * q.oracle
        tgt_usd = targets.get(coin, 0.0) * equity
        delta = tgt_usd - cur_usd
        if abs(delta) < max(config.BAND * equity, config.MIN_ORDER_USD):
            continue
        fills += _trade(broker, coin, delta, cur_usd)
    return fills


def trade_to(broker, coin: str, target_usd: float, quotes: dict[str, Quote]) -> list[dict]:
    """Trade one market to a $ target (0 = close, reduce-only)."""
    q = quotes[coin]
    units = broker.positions().get(coin, 0.0)
    if target_usd == 0:
        return close(broker, coin, units, q.oracle)
    cur_usd = units * q.oracle
    delta = target_usd - cur_usd
    if abs(delta) < config.MIN_ORDER_USD:
        return []
    return _trade(broker, coin, delta, cur_usd)


def close(broker, coin: str, units: float, oracle: float) -> list[dict]:
    """Close an exact position size (in units, so no dust is left)."""
    if units == 0:
        return []
    return _trade(broker, coin, -units * oracle, units * oracle, reduce_only=True,
                  close_units=abs(units))


def flatten(broker, quotes: dict[str, Quote]) -> list[dict]:
    """Close every open position (kill switch, loss stop)."""
    fills = []
    for coin, sz in broker.positions().items():
        q = quotes.get(coin)
        if q is None:
            continue
        fills += close(broker, coin, sz, q.oracle)
    return fills


def _trade(broker, coin: str, delta_usd: float, cur_usd: float,
           reduce_only: bool | None = None, close_units: float | None = None) -> list[dict]:
    """Send delta_usd as slices of at most MAX_ORDER_USD, re-reading the
    book before each slice. Stops early if a slice doesn't fill."""
    is_buy = delta_usd > 0
    # Reduce-only when the trade only shrinks the position (never flips it).
    if reduce_only is None:
        reduce_only = cur_usd != 0 and (cur_usd > 0) != is_buy and abs(delta_usd) <= abs(cur_usd)
    left = abs(delta_usd)
    fills = []
    for _ in range(MAX_SLICES):
        if close_units is None and left < config.MIN_ORDER_USD:
            break
        if close_units is not None and close_units <= 0:
            break
        q = snapshot([coin])[coin]
        if not q.ok():
            log.warning(f"{coin}: book looks wrong mid-trade, stopping")
            break
        ref = q.ask if is_buy else q.bid
        mid = (q.ask + q.bid) / 2
        limit = round_px(mid * (1 + config.MAX_TAKE_BP / 1e4) if is_buy
                         else mid * (1 - config.MAX_TAKE_BP / 1e4), q.sz_decimals)
        usd = min(left, config.MAX_ORDER_USD)
        sz = round(usd / ref, q.sz_decimals)
        if close_units is not None:     # closing: size in units, last slice exact
            sz = round(min(close_units, config.MAX_ORDER_USD / ref), q.sz_decimals)
        elif sz * ref < config.MIN_ORDER_USD:
            break
        if sz <= 0:
            break
        filled, px = broker.order(q, is_buy, sz, limit, reduce_only)
        filled, px = float(filled), float(px)
        f = {"coin": coin, "side": "buy" if is_buy else "sell", "req_sz": sz,
             "filled": filled, "px": px, "oracle": q.oracle,
             "vs_oracle_bp": (px / q.oracle - 1) * 1e4 if filled else None,
             "mode": broker.mode}
        log.info(f"ORDER {f}")
        fills.append(f)
        if filled <= 0:
            break
        left -= filled * px
        if close_units is not None:
            close_units = round(close_units - filled, 10)
        if filled < 0.5 * sz:      # book within our price limit is used up
            log.warning(f"{coin}: only {filled}/{sz} filled, ${left:,.0f} left for next rebalance")
            break
        time.sleep(1)
    if fills:
        fills[-1]["left_usd"] = max(left, 0.0)
    return fills
