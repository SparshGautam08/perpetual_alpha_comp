"""Funding + drift strategy on recorded par data (REPORT_NOTES E13).

    .venv/bin/python -m backtest.drift_strategy

Idea (E7): the organizer bot's quotes sometimes sit far from the oracle,
funding follows that gap, and we are scored at the oracle. So when par's
mid is >= ENTRY bp ABOVE the oracle, sell to the bot (fill above the
oracle, and shorts usually receive funding); when it is >= ENTRY bp BELOW,
buy. Close when the gap is back within EXIT bp, or after MAX_HOLD hours.

Simulation, per market, on the 5 s snapshots:
- fixed $NOTIONAL per trade; fills walk the recorded book (5 levels)
- taker fee FEE_BPS on entry and exit
- funding paid at each hour boundary: position x oracle x recorded rate
- positions are closed at the end of each recorder segment (no holding
  through data gaps we can't see)
P&L of every trade is split into: fill edge vs oracle (entry + exit),
oracle move (market risk), funding, fees.

Data split (fixed Oct 8 2026, before seeing newer data):
- DEV: up to DEV_END. Settings may be tuned here.
- TEST: after DEV_END. Locked settings only.
"""
import itertools

import numpy as np
import pandas as pd

from .costs import FEE_BPS
from .live_market import load

DEV_END = pd.Timestamp("2026-10-07 18:33:23", tz="UTC")
NOTIONAL = 25_000
# Locked Oct 8 2026 (REPORT_NOTES E13), before any test data was seen.
LOCKED = {"entry_bp": 40, "exit_bp": 0, "max_hold_h": 12, "need_funding": True}
GRID = {"entry_bp": [15, 20, 30, 40, 60], "exit_bp": [0, 5, 10],
        "max_hold_h": [1, 4, 12], "need_funding": [False, True]}


def fill_prices(g: pd.DataFrame, usd: float):
    """Average fill price per row for a $usd market buy / sell, walking the
    5 recorded levels. NaN where the book can't fill it."""
    out = {}
    for side in ("ask", "bid"):
        left = np.full(len(g), float(usd))
        units = np.zeros(len(g))
        for i in range(1, 6):
            px = g[f"{side}{i}_px"].to_numpy()
            avail = np.nan_to_num(px * g[f"{side}{i}_sz"].to_numpy())
            take = np.minimum(left, avail)
            units += np.where(px > 0, take / np.where(px > 0, px, 1), 0)
            left -= take
        out[side] = np.where(left <= 1e-6, usd / np.maximum(units, 1e-12), np.nan)
    return out["ask"], out["bid"]    # buy price, sell price


def simulate(g: pd.DataFrame, buy_px, sell_px, entry_bp, exit_bp, max_hold_h,
             need_funding, flip=False) -> list[dict]:
    """One market, one setting. flip=True takes the opposite side of every
    signal (a sanity check: it should lose)."""
    t = g["time"].to_numpy()
    hour = g["time"].dt.floor("h").to_numpy()
    o = g["oraclePx"].to_numpy()
    f = g["funding"].to_numpy()
    gap = ((g["mid"] / g["oraclePx"] - 1) * 1e4).to_numpy()
    seg = g["seg"].to_numpy()
    n = len(g)
    trades, pos = [], None
    max_hold = np.timedelta64(int(max_hold_h * 3600), "s")
    for i in range(n):
        last_in_seg = i == n - 1 or seg[i + 1] != seg[i]
        if pos is not None:
            if i > 0 and hour[i] != hour[i - 1] and seg[i] == seg[i - 1]:
                # Hourly funding: + rate means longs pay.
                pos["funding"] -= pos["side"] * pos["units"] * o[i] * f[i]
            closed = (pos["sig"] > 0 and gap[i] <= exit_bp) or \
                     (pos["sig"] < 0 and gap[i] >= -exit_bp)
            if closed or t[i] - pos["t0"] >= max_hold or last_in_seg:
                px = sell_px[i] if pos["side"] > 0 else buy_px[i]
                if np.isnan(px):
                    continue
                s, q = pos["side"], pos["units"]
                trades.append({
                    "t0": pos["t0"], "t1": t[i], "side": s,
                    "edge_in": s * q * (pos["o0"] - pos["px0"]),
                    "edge_out": s * q * (px - o[i]),
                    "oracle_move": s * q * (o[i] - pos["o0"]),
                    "funding": pos["funding"],
                    "fees": -FEE_BPS / 1e4 * q * (pos["px0"] + px),
                    "why": "gap closed" if closed else
                           ("max hold" if t[i] - pos["t0"] >= max_hold else "data gap")})
                pos = None
            continue
        if last_in_seg:
            continue
        sig = 1 if gap[i] >= entry_bp else (-1 if gap[i] <= -entry_bp else 0)
        if sig == 0:
            continue
        if need_funding and np.sign(f[i]) != sig:   # funding must pay our side
            continue
        side = -sig * (-1 if flip else 1)            # mid above oracle -> sell
        px = buy_px[i] if side > 0 else sell_px[i]
        if np.isnan(px):
            continue
        pos = {"t0": t[i], "side": side, "sig": sig, "units": NOTIONAL / px,
               "px0": px, "o0": o[i], "funding": 0.0}
    for tr in trades:
        tr["net"] = tr["edge_in"] + tr["edge_out"] + tr["oracle_move"] + tr["funding"] + tr["fees"]
    return trades


def prepare(df: pd.DataFrame) -> dict:
    """Per market: its rows and fill prices (computed once, reused per setting)."""
    out = {}
    for coin, g in df.groupby("coin"):
        g = g.reset_index(drop=True)
        out[coin] = (g, *fill_prices(g, NOTIONAL))
    return out


def run(prep: dict, params: dict, flip=False) -> pd.DataFrame:
    rows = []
    for coin, (g, b, s) in prep.items():
        for tr in simulate(g, b, s, flip=flip, **params):
            rows.append({"coin": coin} | tr)
    return pd.DataFrame(rows)


def summarize(tr: pd.DataFrame) -> dict:
    if tr.empty:
        return {"trades": 0, "net_$": 0.0}
    hold_h = (tr.t1 - tr.t0).dt.total_seconds() / 3600
    return {"trades": len(tr), "markets": tr.coin.nunique(),
            "net_$": tr.net.sum(), "net_bp_per_trade": tr.net.mean() / NOTIONAL * 1e4,
            "win_%": 100 * (tr.net > 0).mean(),
            "edge_$": (tr.edge_in + tr.edge_out).sum(), "oracle_move_$": tr.oracle_move.sum(),
            "funding_$": tr.funding.sum(), "fees_$": tr.fees.sum(),
            "avg_hold_h": hold_h.mean()}


def test_locked(test: pd.DataFrame) -> None:
    """Out-of-sample test of the locked rule. Success criterion (E13):
    net P&L excluding oracle moves (edge + funding + fees) > 0."""
    print(f"=== TEST, locked rule {LOCKED} ===")
    tr = run(prepare(test), LOCKED)
    sm = summarize(tr)
    print({k: (round(v, 2) if isinstance(v, float) else v) for k, v in sm.items()})
    if tr.empty:
        print("No trades in the test period.")
        return
    tr = tr.assign(hold_h=(tr.t1 - tr.t0).dt.total_seconds() / 3600,
                   t0=tr.t0.dt.strftime("%m-%d %H:%M"))
    print(tr[["coin", "t0", "side", "hold_h", "edge_in", "edge_out", "oracle_move",
              "funding", "fees", "net", "why"]].to_string(index=False))
    ex = sm["edge_$"] + sm["funding_$"] + sm["fees_$"]
    print(f"\nNet excluding oracle moves: ${ex:,.2f} -> criterion "
          f"{'PASSED' if ex > 0 else 'FAILED'}")
    fl = summarize(run(prepare(test), LOCKED, flip=True))
    print(f"Flipped check: net ${fl.get('net_$', 0):,.2f} over {fl['trades']} trades")


def gap_stats(df: pd.DataFrame, label: str) -> None:
    """How often each market's |mid - oracle| gap was >= 20 / 40 / 60 bp."""
    gap = ((df["mid"] / df["oraclePx"] - 1) * 1e4).abs()
    t = pd.DataFrame({c: [100 * (gap[df.coin == c] >= x).mean() for x in (20, 40, 60)]
                      for c in sorted(df.coin.unique())}, index=["%>=20bp", "%>=40bp", "%>=60bp"]).T
    print(f"\n=== {label}: share of snapshots with |gap| above threshold ===")
    print(t.round(2).to_string())


def main():
    import sys
    pd.set_option("display.width", 240)
    pd.set_option("display.float_format", "{:.2f}".format)
    df = load()
    dev = df[df.time <= DEV_END]
    test = df[df.time > DEV_END]
    print(f"DEV rows {len(dev):,} ({dev.time.min():%m-%d %H:%M} -> {dev.time.max():%m-%d %H:%M} UTC); "
          f"TEST rows {len(test):,}")
    print(f"Notional ${NOTIONAL:,} per trade, fee {FEE_BPS} bp/side\n")
    if "--test" in sys.argv:
        gap_stats(dev, "DEV")
        gap_stats(test, "TEST")
        print()
        test_locked(test)
        return

    prep = prepare(dev)
    keys = list(GRID)
    rows = []
    for combo in itertools.product(*GRID.values()):
        p = dict(zip(keys, combo))
        if p["exit_bp"] >= p["entry_bp"]:
            continue
        rows.append(p | summarize(run(prep, p)))
    grid = pd.DataFrame(rows).sort_values("net_$", ascending=False)
    print("=== DEV grid (top 15 by net $) ===")
    print(grid.head(15).to_string(index=False))
    print("\n=== DEV grid: net $ by entry threshold (median over other settings) ===")
    print(grid.groupby("entry_bp")[["net_$", "edge_$", "oracle_move_$", "funding_$", "fees_$", "trades"]]
          .median().to_string())
    grid.to_csv("results/drift_grid_dev.csv", index=False)


if __name__ == "__main__" and "--v2" not in __import__("sys").argv:
    main()


# ---------------------------------------------------------------------------
# v2 (E16): signal measured at the EXECUTABLE price for our size, not the
# top-of-book mid (v1 failed out of sample because small orders from other
# traders distort the mid, E15).
# Data split for v2: design on everything up to V2_DEV_END (all data seen
# while diagnosing v1); test on data after it, with LOCKED_V2 only.
V2_DEV_END = pd.Timestamp("2026-10-09 03:05:00", tz="UTC")
GRID_V2 = {"entry_edge_bp": [15, 20, 30, 40], "exit_cost_bp": [5, 10, 15],
           "max_hold_h": [4, 12], "need_funding": [False, True]}


def simulate_v2(g, buy_px, sell_px, entry_edge_bp, exit_cost_bp, max_hold_h,
                need_funding, flip=False, stop_bp=None, pred=None,
                block_sell=None, block_buy=None, mark_open_at_end=False) -> list[dict]:
    """Enter when a $NOTIONAL fill is >= entry_edge_bp better than the
    oracle (sell above it / buy below it); exit when closing would cost at
    most exit_cost_bp vs the oracle, or after max_hold_h.
    pred (E39): optional predicted oracle move per row (bp); entries then
    use the edge adjusted by it (short: sell_edge - pred, long: buy_edge + pred).
    block_sell / block_buy (E45): optional bool per row; True forbids that
    entry side there (e.g. right after an aggressive sweep in that direction).
    mark_open_at_end (E45): a position that cannot be closed at a segment's
    end (book too thin for the exit) is closed at the oracle there instead of
    being dropped (the default keeps earlier results unchanged)."""
    t = g["time"].to_numpy()
    hour = g["time"].dt.floor("h").to_numpy()
    o = g["oraclePx"].to_numpy()
    f = g["funding"].to_numpy()
    seg = g["seg"].to_numpy()
    sell_edge = (sell_px / o - 1) * 1e4          # + = we can sell above oracle
    buy_edge = (1 - buy_px / o) * 1e4            # + = we can buy below oracle
    n, trades, pos = len(g), [], None
    max_hold = np.timedelta64(int(max_hold_h * 3600), "s")
    for i in range(n):
        last_in_seg = i == n - 1 or seg[i + 1] != seg[i]
        if pos is not None:
            if i > 0 and hour[i] != hour[i - 1] and seg[i] == seg[i - 1]:
                pos["funding"] -= pos["side"] * pos["units"] * o[i] * f[i]
            # Cost of closing now vs oracle: short closes by buying, long by selling.
            close_cost = -buy_edge[i] if pos["side"] < 0 else -sell_edge[i]
            cheap = close_cost <= exit_cost_bp
            old = t[i] - pos["t0"] >= max_hold
            # Optional stop (E31): loss marked at the oracle vs our entry price.
            stopped = stop_bp is not None and \
                pos["side"] * (o[i] / pos["px0"] - 1) * 1e4 <= -stop_bp
            if cheap or old or stopped or last_in_seg:
                px = sell_px[i] if pos["side"] > 0 else buy_px[i]
                if np.isnan(px):
                    if not (mark_open_at_end and last_in_seg):
                        continue
                    px = o[i]                        # unclosable at the end: mark at the oracle
                s, q = pos["side"], pos["units"]
                trades.append({
                    "t0": pos["t0"], "t1": t[i], "side": s,
                    "edge_in": s * q * (pos["o0"] - pos["px0"]),
                    "edge_out": s * q * (px - o[i]),
                    "oracle_move": s * q * (o[i] - pos["o0"]),
                    "funding": pos["funding"],
                    "fees": -FEE_BPS / 1e4 * q * (pos["px0"] + px),
                    "why": "cheap exit" if cheap else ("max hold" if old else
                                                        ("stop" if stopped else "data gap"))})
                pos = None
            continue
        if last_in_seg:
            continue
        side = 0
        p_i = 0.0 if pred is None or np.isnan(pred[i]) else pred[i]
        if sell_edge[i] - p_i >= entry_edge_bp:
            side = -1
        elif buy_edge[i] + p_i >= entry_edge_bp:
            side = 1
        if side == 0:
            continue
        if (side < 0 and block_sell is not None and block_sell[i]) or \
                (side > 0 and block_buy is not None and block_buy[i]):
            continue
        if need_funding and np.sign(f[i]) != -side:   # shorts paid when f > 0
            continue
        if flip:
            side = -side
        px = buy_px[i] if side > 0 else sell_px[i]
        if np.isnan(px):
            continue
        pos = {"t0": t[i], "side": side, "units": NOTIONAL / px, "px0": px,
               "o0": o[i], "funding": 0.0}
    for tr in trades:
        tr["net"] = tr["edge_in"] + tr["edge_out"] + tr["oracle_move"] + tr["funding"] + tr["fees"]
    return trades


def run_v2(prep: dict, params: dict, flip=False) -> pd.DataFrame:
    rows = []
    for coin, (g, b, s) in prep.items():
        for tr in simulate_v2(g, b, s, flip=flip, **params):
            rows.append({"coin": coin} | tr)
    return pd.DataFrame(rows)


LOCKED_V2 = {"entry_edge_bp": 40, "exit_cost_bp": 10, "max_hold_h": 12, "need_funding": False}


def test_v2() -> None:
    """Out-of-sample test of the locked v2 rule on data after V2_DEV_END.
    Success criterion (E16): net excluding oracle moves > 0."""
    pd.set_option("display.width", 240)
    pd.set_option("display.float_format", "{:.2f}".format)
    df = load()
    test = df[df.time > V2_DEV_END]
    print(f"v2 TEST: {test.time.min():%m-%d %H:%M} -> {test.time.max():%m-%d %H:%M} UTC, "
          f"{len(test):,} rows, rule {LOCKED_V2}")
    prep = prepare(test)
    tr = run_v2(prep, LOCKED_V2)
    sm = summarize(tr)
    print({k: (round(v, 2) if isinstance(v, float) else v) for k, v in sm.items()})
    if not tr.empty:
        tr = tr.assign(hold_h=(tr.t1 - tr.t0).dt.total_seconds() / 3600,
                       t0=tr.t0.dt.strftime("%m-%d %H:%M"))
        print(tr[["coin", "t0", "side", "hold_h", "edge_in", "edge_out", "oracle_move",
                  "funding", "fees", "net", "why"]].to_string(index=False))
        ex = (tr.edge_in + tr.edge_out + tr.funding + tr.fees).sum()
        print(f"\nNet excluding oracle moves: ${ex:,.2f} -> criterion {'PASSED' if ex > 0 else 'FAILED'}")
        fl = summarize(run_v2(prep, LOCKED_V2, flip=True))
        print(f"Flipped check: net ${fl.get('net_$', 0):,.2f} over {fl['trades']} trades")
    else:
        print("No trades in the test period.")


def main_v2():
    pd.set_option("display.width", 240)
    pd.set_option("display.float_format", "{:.2f}".format)
    df = load()
    dev = df[df.time <= V2_DEV_END]
    print(f"v2 DEV: {dev.time.min():%m-%d %H:%M} -> {dev.time.max():%m-%d %H:%M} UTC, "
          f"{len(dev):,} rows; split at Oct 7 18:33 into early/late halves too\n")
    prep_all = prepare(dev)
    split = pd.Timestamp("2026-10-07 18:33:23", tz="UTC")
    prep_a = prepare(dev[dev.time <= split])
    prep_b = prepare(dev[dev.time > split])
    rows = []
    for combo in itertools.product(*GRID_V2.values()):
        p = dict(zip(GRID_V2, combo))
        tr = run_v2(prep_all, p)
        sm = summarize(tr)
        ex = lambda t: 0.0 if t.empty else (t.edge_in + t.edge_out + t.funding + t.fees).sum()
        rows.append(p | sm | {"ex_oracle_$": ex(tr),
                              "ex_oracle_early_$": ex(run_v2(prep_a, p)),
                              "ex_oracle_late_$": ex(run_v2(prep_b, p))})
    grid = pd.DataFrame(rows).sort_values("ex_oracle_$", ascending=False)
    cols = list(GRID_V2) + ["trades", "net_$", "ex_oracle_$", "ex_oracle_early_$",
                            "ex_oracle_late_$", "edge_$", "funding_$", "fees_$", "avg_hold_h"]
    print("=== v2 DEV grid, sorted by net EXCLUDING oracle moves ===")
    print(grid[cols].head(20).to_string(index=False))
    print("\n=== median net excl. oracle moves by entry edge ===")
    print(grid.groupby("entry_edge_bp")[["ex_oracle_$", "ex_oracle_early_$", "ex_oracle_late_$",
                                         "trades"]].median().to_string())
    grid.to_csv("results/drift_v2_grid_dev.csv", index=False)


if __name__ == "__main__" and "--v2" in __import__("sys").argv:
    test_v2() if "--test" in __import__("sys").argv else main_v2()
