"""E30: why did live drift earn much less than the backtest over the first
weekend (E27: backtest v2 +$1,117 vs live ~+$200)?

    .venv/bin/python -m backtest.live_vs_backtest

Live: the team account's actual fills (exchange API), grouped into
~$25k drift legs and paired into round trips; risk parity rebalances (small
or at 00:00 UTC) and the E26 orphan are excluded. The oracle at each fill
time comes from the 5 s recorder. Backtest: the locked v2 rule on the same
recorded data. Both are split the same way: entry edge vs oracle, exit
edge vs oracle, oracle move, fees.
Trades are matched by market and entry time (within MATCH_MIN minutes).
"""
from collections import defaultdict

import numpy as np
import pandas as pd
import requests

from .costs import FEE_BPS
from .drift_strategy import LOCKED_V2, load, prepare, run_v2

TEAM = "0xC9f1C5a784d2cd5858396299893e71cfe23F4aAf"
API = "https://api.hyperliquid-testnet.xyz/info"
START = pd.Timestamp("2026-10-09 23:09", tz="UTC")
END = pd.Timestamp("2026-10-13 00:02", tz="UTC")
MATCH_MIN = 10
ORPHAN = pd.Timestamp("2026-10-12 03:03", tz="UTC")   # E26 double exit


def live_round_trips(oracle_of) -> pd.DataFrame:
    fills = requests.post(API, json={"type": "userFills", "user": TEAM}, timeout=20).json()
    fills = sorted((x for x in fills if START.value // 10**6 <= x["time"] <= END.value // 10**6),
                   key=lambda x: x["time"])
    legs = defaultdict(list)
    for x in fills:
        c, s = x["coin"].split(":")[1], 1 if x["side"] == "B" else -1
        L = legs[c]
        if L and L[-1]["s"] == s and x["time"] - L[-1]["t1"] < 180_000:
            o = L[-1]
        else:
            o = {"s": s, "t0": x["time"], "t1": x["time"], "sz": 0.0, "usd": 0.0, "fee": 0.0}
            L.append(o)
        o["sz"] += float(x["sz"]); o["usd"] += float(x["sz"]) * float(x["px"])
        o["fee"] += float(x["fee"]); o["t1"] = x["time"]
    rows = []
    for c, L in legs.items():
        L = [o for o in L if 20_000 <= o["usd"] <= 30_000
             and pd.Timestamp(o["t0"], unit="ms", tz="UTC").hour != 0          # RP rebalance hour
             and abs(pd.Timestamp(o["t0"], unit="ms", tz="UTC") - ORPHAN) > pd.Timedelta(minutes=2)]
        i = 0
        while i + 1 < len(L):
            a, z = L[i], L[i + 1]
            if a["s"] != -z["s"]:
                i += 1
                continue
            t0 = pd.Timestamp(a["t0"], unit="ms", tz="UTC")
            t1 = pd.Timestamp(z["t0"], unit="ms", tz="UTC")
            q = min(a["sz"], z["sz"])
            p0, p1 = a["usd"] / a["sz"], z["usd"] / z["sz"]
            o0, o1 = oracle_of(c, t0), oracle_of(c, t1)
            s = a["s"]
            rows.append({"coin": c, "t0": t0, "t1": t1, "side": s,
                         "edge_in": s * q * (o0 - p0), "edge_out": s * q * (p1 - o1),
                         "oracle_move": s * q * (o1 - o0), "funding": 0.0,
                         "fees": -(a["fee"] + z["fee"])})
            i += 2
    t = pd.DataFrame(rows)
    t["net"] = t[["edge_in", "edge_out", "oracle_move", "funding", "fees"]].sum(axis=1)
    return t


def split(t: pd.DataFrame) -> dict:
    return {"trades": len(t), "entry_edge": t.edge_in.sum(), "exit_edge": t.edge_out.sum(),
            "oracle_move": t.oracle_move.sum(), "fees": t.fees.sum(), "net": t.net.sum(),
            "entry_bp_per_trade": (t.edge_in / 25_000 * 1e4).mean(),
            "exit_bp_per_trade": (t.edge_out / 25_000 * 1e4).mean(),
            "hold_min": ((t.t1 - t.t0).dt.total_seconds() / 60).median()}


def main():
    pd.set_option("display.width", 230)
    pd.set_option("display.float_format", "{:.1f}".format)
    df = load()
    df = df[(df.time > START) & (df.time <= END)]
    orc = {c: g.set_index("time")["oraclePx"].sort_index() for c, g in df.groupby("coin")}
    oracle_of = lambda c, t: float(orc[c].asof(t))

    live = live_round_trips(oracle_of)
    bt = run_v2(prepare(df), LOCKED_V2)
    print(f"Window {START:%a %m-%d %H:%M} -> {END:%a %m-%d %H:%M} UTC (rule v2, 40 bp, $25k)\n")
    print(pd.DataFrame({"live": split(live), "backtest": split(bt)}).to_string())

    # Match by market + entry time
    used, rows = set(), []
    for i, b in bt.iterrows():
        cand = live[(live.coin == b.coin) & ((live.t0 - b.t0).abs() <= pd.Timedelta(minutes=MATCH_MIN))]
        cand = cand[~cand.index.isin(used)]
        if len(cand):
            j = (cand.t0 - b.t0).abs().idxmin()
            used.add(j)
            l = live.loc[j]
            rows.append({"coin": b.coin, "bt_entry": b.t0.strftime("%a %H:%M"),
                         "live_delay_s": (l.t0 - b.t0).total_seconds(),
                         "bt_entry_bp": b.edge_in / 25_000 * 1e4, "live_entry_bp": l.edge_in / 25_000 * 1e4,
                         "bt_exit_bp": b.edge_out / 25_000 * 1e4, "live_exit_bp": l.edge_out / 25_000 * 1e4,
                         "bt_net": b.net, "live_net": l.net})
        else:
            rows.append({"coin": b.coin, "bt_entry": b.t0.strftime("%a %H:%M"), "live_delay_s": np.nan,
                         "bt_entry_bp": b.edge_in / 25_000 * 1e4, "bt_net": b.net})
    m = pd.DataFrame(rows)
    print(f"\n=== Backtest trades matched to live (within {MATCH_MIN} min) ===")
    print(m.to_string(index=False))
    missed = m[m.live_delay_s.isna()]
    extra = live[~live.index.isin(used)]
    print(f"\nBacktest trades live MISSED: {len(missed)} (backtest net ${missed.bt_net.sum():,.0f})")
    print(f"Live trades NOT in backtest: {len(extra)} (live net ${extra.net.sum():,.0f})")
    if len(extra):
        print(extra.assign(t0=extra.t0.dt.strftime("%a %H:%M"))[["coin", "t0", "side", "edge_in", "edge_out",
                                                                  "oracle_move", "fees", "net"]].to_string(index=False))
    both = m.dropna(subset=["live_delay_s"])
    if len(both):
        print(f"\nMatched ({len(both)}): median live delay {both.live_delay_s.median():.0f} s; "
              f"entry edge bt {both.bt_entry_bp.mean():.1f} bp vs live {both.live_entry_bp.mean():.1f} bp; "
              f"exit bt {both.bt_exit_bp.mean():.1f} bp vs live {both.live_exit_bp.mean():.1f} bp; "
              f"net bt ${both.bt_net.sum():,.0f} vs live ${both.live_net.sum():,.0f}")


if __name__ == "__main__":
    main()
