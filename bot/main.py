"""The trading bot. Paper mode by default.

    python -m bot.main                     # paper mode, runs forever
    python -m bot.main --rebalance-now     # paper, rebalance immediately once
    BOT_LIVE=yes python -m bot.main --mode live   # real orders (needs HL_API_KEY_FILE)

Every LOOP_SECONDS it:
1. reads quotes and equity (at oracle, what we're scored on);
2. checks the kill switch (state/STOP -> flatten and exit), pause file
   (state/PAUSE -> no orders) and the daily loss stop (flatten, no more
   trading until the next UTC day);
3. once a day at REBAL_HOUR_UTC, asks the strategy for target weights and
   trades toward them.
Errors are logged and alerted; the loop keeps going with a backoff.
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

from . import config
from .alerts import alert
from .broker import LiveBroker, PaperBroker
from .execution import flatten, rebalance, trade_to
from .market import snapshot
from .portfolio import Combined
from .strategy import Drift, RiskParity

log = logging.getLogger("bot")


def setup_logging():
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fmt.converter = time.gmtime
    for h in (logging.StreamHandler(sys.stdout),
              logging.FileHandler(config.STATE_DIR / "bot.log")):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False   # the SDK configures the root logger; avoid double lines


class State:
    """Small persistent state so restarts don't lose the day's bookkeeping."""

    def __init__(self, path):
        self.path = path
        self.d = json.loads(path.read_text()) if path.exists() else {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, **kw):
        self.d.update(kw)
        self.path.write_text(json.dumps(self.d, indent=2))


def held_markets(broker, extra) -> list[str]:
    return sorted(set(config.MARKETS) | set(broker.positions()) | set(extra))


def drift_step(broker, strategy, quotes, state) -> None:
    """Drift strategy: check every market every loop, trade on entries/exits."""
    for coin, (target, reason) in strategy.tick(quotes, broker.positions(), time.time()).items():
        log.info(f"DRIFT {coin}: {reason} -> target ${target:,.0f}")
        fills = trade_to(broker, coin, target, quotes)
        done = sum(f["filled"] for f in fills) > 0
        if done:
            strategy.record(coin, time.time(), opened=target != 0)
            alert(f"drift {'entry' if target else 'exit'} {coin}", reason, priority="low",
                  key=f"drift-{coin}", every_s=60)


def step(broker, strategy, state: State, force_rebalance: bool) -> bool:
    """One loop iteration. Returns False when the bot should exit."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    quotes = snapshot(held_markets(broker, []))
    broker.accrue_funding(quotes)
    equity = broker.equity_oracle(quotes)

    # New UTC day: record starting equity for the loss stop, send a summary.
    if state.get("day") != today:
        prev = state.get("day_start_equity")
        if prev:
            alert("daily summary", f"{state.get('day')}: equity {equity:,.0f} "
                  f"({(equity / prev - 1) * 100:+.2f}%), positions {broker.positions()}",
                  priority="low", key="summary")
        state.set(day=today, day_start_equity=equity)

    # Kill switch: flatten and exit.
    if config.STOP_FILE.exists():
        fills = flatten(broker, quotes)
        if isinstance(strategy, Combined):
            strategy.reset()
        alert("STOPPED", f"STOP file found. Flattened {len(fills)} orders. "
              f"Positions now {broker.positions()}", priority="urgent")
        return False

    # Daily loss stop.
    start_eq = state.get("day_start_equity")
    if state.get("halted_day") == today:
        return True
    if start_eq and equity < start_eq * (1 - config.DAILY_LOSS_STOP):
        fills = flatten(broker, quotes)
        if isinstance(strategy, Combined):
            strategy.reset()
        state.set(halted_day=today)
        alert("loss stop", f"Equity {equity:,.0f} is {(equity / start_eq - 1) * 100:.2f}% "
              f"on the day. Flattened ({len(fills)} orders); no trading until tomorrow UTC.",
              priority="urgent")
        return True

    if config.PAUSE_FILE.exists():
        return True

    if config.REBALANCE_FILE.exists():
        force_rebalance = True
        config.REBALANCE_FILE.unlink()
        log.info("REBALANCE file found: rebalancing now")

    if isinstance(strategy, Combined):
        due = (now.hour == config.REBAL_HOUR_UTC and state.get("last_rebalance_day") != today) \
            or force_rebalance
        quotes = snapshot(held_markets(broker, strategy.markets))
        strategy.step(broker, quotes, equity, due)
        if due:
            state.set(last_rebalance_day=today)
        equity = broker.equity_oracle(quotes)
        log.info(f"HEARTBEAT {broker.mode} {strategy.name} equity@oracle {equity:,.2f} "
                 f"positions {json.dumps({k: round(v, 6) for k, v in broker.positions().items()})} "
                 f"drift_units {json.dumps(state.get('drift_units', {}))}")
        return True

    if isinstance(strategy, Drift):
        quotes = snapshot(held_markets(broker, Drift.MARKETS))
        drift_step(broker, strategy, quotes, state)
        equity = broker.equity_oracle(quotes)
        log.info(f"HEARTBEAT {broker.mode} {strategy.name} equity@oracle {equity:,.2f} "
                 f"positions {json.dumps({k: round(v, 6) for k, v in broker.positions().items()})}")
        return True

    due = (now.hour == config.REBAL_HOUR_UTC and state.get("last_rebalance_day") != today)
    if due or force_rebalance:
        targets = strategy.target_weights()
        quotes = snapshot(held_markets(broker, targets))
        log.info(f"REBALANCE equity {equity:,.0f} targets "
                 + json.dumps({k: round(v, 4) for k, v in targets.items()}))
        fills = rebalance(broker, targets, quotes, equity)
        state.set(last_rebalance_day=today, last_targets=targets)
        short = {f["coin"]: round(f["left_usd"]) for f in fills
                 if f.get("left_usd", 0) >= config.MIN_ORDER_USD}
        log.info(f"Rebalanced: {len(fills)} orders, short of target ($): {short or 'none'}")
        if short:
            alert("partial rebalance", f"Could not reach target, $ left: {short}",
                  priority="default", key="short")
        equity = broker.equity_oracle(snapshot(held_markets(broker, [])))

    log.info(f"HEARTBEAT {broker.mode} equity@oracle {equity:,.2f} "
             f"positions {json.dumps({k: round(v, 6) for k, v in broker.positions().items()})}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["paper", "live"], default="paper")
    ap.add_argument("--rebalance-now", action="store_true",
                    help="rebalance once on the first loop, then follow the schedule")
    ap.add_argument("--once", action="store_true", help="run one loop and exit")
    ap.add_argument("--strategy", choices=["riskparity", "drift", "combined"],
                    default="riskparity")
    a = ap.parse_args()
    setup_logging()

    # state per dex, so positions remembered on par never carry over to the new dex
    tag = "" if config.DEX == "par" else f"{config.DEX}_"
    if a.mode == "live":
        if os.environ.get("BOT_LIVE") != "yes" or not config.KEY_FILE:
            raise SystemExit("Live mode needs BOT_LIVE=yes and HL_API_KEY_FILE set.")
        broker = LiveBroker()
    else:
        broker = PaperBroker(name=f"paper_{tag}{a.strategy}")
    state = State(config.STATE_DIR / f"state_{tag}{a.mode}_{a.strategy}.json")
    strategy = {"drift": lambda: Drift(state), "combined": lambda: Combined(state),
                "riskparity": RiskParity}[a.strategy]()
    alert("started", f"{a.mode} mode, strategy {strategy.name}", priority="low")

    force, errors = a.rebalance_now, 0
    while True:
        try:
            if not step(broker, strategy, state, force):
                break
            force = False
            if errors >= 3:      # only if an error alert actually went out
                alert("recovered", f"Working again after {errors} failed loop(s)", key="recovered")
            errors = 0
        except Exception as e:
            errors += 1
            log.exception("Loop failed")
            if errors >= 3:
                alert("errors", f"{errors} loops in a row failed: {type(e).__name__}: {e}",
                      priority="high", key="errors")
        if a.once:
            break
        time.sleep(min(config.LOOP_SECONDS * (2 ** min(errors, 4)), 900))
    log.info("Bot exited.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped by Ctrl+C")
    except SystemExit:
        raise
    except Exception as e:
        alert("CRASHED", f"{type(e).__name__}: {e}", priority="urgent")
        raise
