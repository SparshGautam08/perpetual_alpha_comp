"""Replay one market's recorded order book around a moment (e.g. a fill).

    python -m backtest.book_at ~/data_pas pas:BRENT "2026-10-20 15:12:27" [--before 15 --after 5]

Reads the record_ws.py files of that UTC day (l2_, ctxws_, trades_). For each
book snapshot in the window it prints the oracle and, per side, how many $
could be filled (a) at prices at least ENTRY_BP better than the oracle (what
drift's signal accepts) and (b) within LIMIT_BP of the mid (what the
execution's price limit accepts), plus the top levels. Then every trade in
the window with its buyer/seller, our team marked. Used for E44 (why a drift
entry filled only partly).
"""
import argparse
from pathlib import Path

import pandas as pd

TEAM = "0xc9f1c5a784d2cd5858396299893e71cfe23f4aaf"
ENTRY_BP = 30
LIMIT_BP = 15
LEVELS = 20


def read_window(path, coin, t0, t1, chunks=200_000):
    if not path.exists():
        print(f"(missing: {path.name})")
        return pd.DataFrame(columns=["recv_ms", "coin"])
    out = []
    for c in pd.read_csv(path, chunksize=chunks):
        c = c[(c.coin == coin) & (c.recv_ms >= t0) & (c.recv_ms <= t1)]
        if len(c):
            out.append(c)
    return pd.concat(out) if out else pd.DataFrame(columns=["recv_ms", "coin"])


def fillable(row, side, ok):
    """$ available on one side at levels whose price passes ok(px)."""
    usd = 0.0
    for i in range(1, LEVELS + 1):
        px, sz = row.get(f"{side}{i}_px"), row.get(f"{side}{i}_sz")
        if pd.isna(px):
            break
        if ok(px):
            usd += px * sz
    return usd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("coin")
    ap.add_argument("time", help='UTC, e.g. "2026-10-20 15:12:27"')
    ap.add_argument("--before", type=float, default=15)
    ap.add_argument("--after", type=float, default=5)
    a = ap.parse_args()
    t = pd.Timestamp(a.time, tz="UTC")
    t0, t1 = int((t.timestamp() - a.before) * 1000), int((t.timestamp() + a.after) * 1000)
    day = t.strftime("%Y-%m-%d")
    folder = Path(a.folder).expanduser()

    ctx = read_window(folder / f"ctxws_{day}.csv", a.coin, t0 - 60_000, t1)
    l2 = read_window(folder / f"l2_{day}.csv", a.coin, t0, t1)
    trades = read_window(folder / f"trades_{day}.csv", a.coin, t0, t1)
    oracle = ctx.set_index("recv_ms").oraclePx.sort_index() if len(ctx) else pd.Series(dtype=float)
    fmt = lambda ms: pd.Timestamp(ms, unit="ms", tz="UTC").strftime("%H:%M:%S.%f")[:-3]

    print(f"{a.coin} book, {fmt(t0)} -> {fmt(t1)} UTC: {len(l2)} snapshots, {len(trades)} trades\n")
    for _, r in l2.sort_values("recv_ms").iterrows():
        o = oracle[oracle.index <= r.recv_ms].iloc[-1] if (oracle.index <= r.recv_ms).any() else float("nan")
        mid = (r.bid1_px + r.ask1_px) / 2
        sell_sig = fillable(r, "bid", lambda p: p >= o * (1 + ENTRY_BP / 1e4))
        sell_lim = fillable(r, "bid", lambda p: p >= mid * (1 - LIMIT_BP / 1e4))
        buy_sig = fillable(r, "ask", lambda p: p <= o * (1 - ENTRY_BP / 1e4))
        buy_lim = fillable(r, "ask", lambda p: p <= mid * (1 + LIMIT_BP / 1e4))
        top = lambda s: ", ".join(f"{r[f'{s}{i}_px']}x{r[f'{s}{i}_sz']}" for i in range(1, 4)
                                  if not pd.isna(r.get(f"{s}{i}_px")))
        print(f"{fmt(r.recv_ms)} oracle {o}  mid {mid:.6g} ({(mid / o - 1) * 1e4:+.0f} bp)  "
              f"SELL: ${sell_sig:,.0f} >= +{ENTRY_BP} bp vs oracle, ${sell_lim:,.0f} within {LIMIT_BP} bp of mid | "
              f"BUY: ${buy_sig:,.0f} / ${buy_lim:,.0f}")
        print(f"             bids {top('bid')}   asks {top('ask')}")
    if len(trades):
        print("\nTrades:")
        for _, r in trades.sort_values("recv_ms").iterrows():
            who = lambda x: "US" if str(x).lower() == TEAM else str(x)[:10]
            print(f"  {fmt(r.recv_ms)} {r.side} {r.sz} @ {r.px}  buyer {who(r.buyer)}  seller {who(r.seller)}")


if __name__ == "__main__":
    main()
