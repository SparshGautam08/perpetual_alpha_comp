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


def main():
    pd.set_option("display.width", 240)
    pd.set_option("display.float_format", "{:.2f}".format)
    df = load()
    dev = df[df.time <= DEV_END]
    test = df[df.time > DEV_END]
    print(f"DEV rows {len(dev):,} ({dev.time.min():%m-%d %H:%M} -> {dev.time.max():%m-%d %H:%M} UTC); "
          f"TEST rows {len(test):,}")
    print(f"Notional ${NOTIONAL:,} per trade, fee {FEE_BPS} bp/side\n")

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


if __name__ == "__main__":
    main()
