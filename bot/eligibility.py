"""Eligibility safeguard: make sure the team has >= 1 fill in the scored phase.

Rules (About page): "To be ranked, a team must have at least one executed
trade (fill) during the scored phase." Drift trades most days, but on a very
quiet dex it could go days without a trade. If ELIG_TRADE_BY is set and,
once that time has passed, the account has no fill on the dex since
SCORED_START, the bot makes ONE tiny algorithmic round trip (~ELIG_USD in a
market it does not hold: buy, then close exactly what was bought) and never
acts again. Off when ELIG_TRADE_BY is empty (the default).
"""
import logging
import time
from datetime import datetime

from . import config
from .alerts import alert
from .execution import _trade, close
from .market import post, snapshot

log = logging.getLogger("bot")
CHECK_EVERY_S = 600
COINS = ["BTC", "ETH", "SP500", "GOLD", "HYPE"]     # most liquid first


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def scored_fills() -> int:
    """The team's fills on this dex since the scored phase started."""
    fills = post(config.BASE_URL, {"type": "userFillsByTime", "user": config.TEAM,
                                   "startTime": int(_ts(config.SCORED_START) * 1000)})
    return sum(1 for f in fills if f.get("coin", "").startswith(f"{config.DEX}:"))


def check(broker, state, now_s: float | None = None) -> None:
    if not config.ELIG_TRADE_BY or state.get("elig_done"):
        return
    now_s = now_s or time.time()
    if now_s < _ts(config.ELIG_TRADE_BY) or now_s - state.get("elig_checked", 0) < CHECK_EVERY_S:
        return
    state.set(elig_checked=now_s)
    n = scored_fills()
    if n:
        log.info(f"Eligibility: {n} fill(s) on {config.DEX} since {config.SCORED_START}; nothing to do")
        state.set(elig_done=True)
        return
    pos = broker.positions()
    coin = next((c for c in COINS if not pos.get(c)), None)
    if coin is None:
        log.warning("Eligibility: holding every candidate market; trying again later")
        return
    quotes = snapshot([coin])
    if not quotes[coin].ok():
        log.warning(f"Eligibility: {coin} book looks wrong; trying again later")
        return
    log.info(f"Eligibility: no fill on {config.DEX} since {config.SCORED_START}; "
             f"round trip ~${config.ELIG_USD:.0f} in {coin}")
    _trade(broker, coin, config.ELIG_USD, 0.0)
    bought = broker.positions().get(coin, 0.0) - pos.get(coin, 0.0)
    if bought:
        close(broker, coin, bought, quotes[coin].oracle)
        state.set(elig_done=True)
        alert("eligibility trade", f"Made the eligibility round trip in {coin} "
              f"({bought} units); position now {broker.positions().get(coin, 0.0)}", priority="high")
    else:
        log.warning("Eligibility: the opening order did not fill; trying again later")
