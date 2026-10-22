"""E45: drift on the live dex (pas), from our own recordings.

    python -m backtest.pas_drift ~/data_pas ~/shadow

Inputs (record_ws.py on pas, bot VM): ctxws_ (oracle, funding), l2_ (book),
trades_ (every trade, aggressor side, buyer/seller); shadow_ (ML Model 1's
prediction per market per second). The book is sampled every CHECK_S
seconds like the live bot (latest l2 and ctx at or before each check), and
drift v2b runs on it through backtest.drift_strategy.simulate_v2 (same code
as all earlier drift tests).

Settings tried:
- entry threshold: 20, 25, 30 (live), 40 bp
- ML filter: off / on (E39 rule: edge adjusted by the predicted 15 s oracle
  move)
- sweep filter (E44 idea): off, or no entry in the direction of an
  aggressive sweep of >= $5k / $20k by other accounts in the last 10 s
  (a buy sweep pushes the book above the oracle, i.e. creates sell signals)

LOCKED before any result was seen (E45, committed with this script):
- DESIGN = Oct 19 12:00 -> Oct 20 12:00 UTC (scored day 1); TEST = after
  Oct 20 12:00 UTC (to the end of the data).
- Choice: among settings with >= 5 design trades, the highest design net.
- Adopt it live only if on TEST: net > 0 AND net >= the live setting's net
  (30 bp, ML on, no sweep filter). Otherwise keep the live setting.
"""
import os
import sys
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

from .drift_strategy import fill_prices, simulate_v2

# (env overrides only for testing the code on older data)
START = pd.Timestamp(os.environ.get("PAS_START", "2026-10-19 12:00"), tz="UTC")
SPLIT = pd.Timestamp(os.environ.get("PAS_SPLIT", "2026-10-20 12:00"), tz="UTC")
CHECK_S = 15
GAP_S = 120              # no ctx row from any market for this long = recorder gap
SWEEP_S = 10
TEAM = "0xc9f1c5a784d2cd5858396299893e71cfe23f4aaf"
NOTIONAL = 25_000
LIVE = (30, True, None)
BASE = {"exit_cost_bp": 10, "max_hold_h": 12, "need_funding": False, "stop_bp": 200,
        "mark_open_at_end": True}
GRID = list(product([20, 25, 30, 40], [False, True], [None, 5_000, 20_000]))


def read(folder, prefix, cols):
    files = sorted(Path(folder).expanduser().glob(f"{prefix}_*.csv"))
    files = [f for f in files if f.stem.split("_")[-1] >= (START - pd.Timedelta(days=1)).strftime("%Y-%m-%d")]
    return pd.concat([pd.read_csv(f, usecols=cols) for f in files], ignore_index=True)


def load(folder):
    lv = [f"{s}{i}_{k}" for s in ("bid", "ask") for i in range(1, 6) for k in ("px", "sz")]
    ctx = read(folder, "ctxws", ["recv_ms", "coin", "oraclePx", "funding"]).sort_values("recv_ms")
    l2 = read(folder, "l2", ["recv_ms", "coin"] + lv).sort_values("recv_ms")
    tr = read(folder, "trades", ["recv_ms", "coin", "side", "px", "sz", "buyer", "seller"])
    t0 = int(START.timestamp() * 1000)
    t1 = int(min(ctx.recv_ms.max(), l2.recv_ms.max()))
    grid = np.arange(t0, t1, CHECK_S * 1000)
    # recorder gaps: any stretch with no ctx row from any market
    all_ctx = ctx.recv_ms.to_numpy()
    last = all_ctx[np.searchsorted(all_ctx, grid, side="right") - 1]
    stale = (grid - last) > GAP_S * 1000
    rows = []
    for coin in sorted(l2.coin.unique()):
        g = pd.DataFrame({"recv_ms": grid})
        g = pd.merge_asof(g, l2[l2.coin == coin].drop(columns="coin"), on="recv_ms")
        g = pd.merge_asof(g, ctx[ctx.coin == coin].drop(columns="coin"), on="recv_ms")
        g["coin"] = coin.split(":", 1)[1]
        g["stale"] = stale
        rows.append(g)
    df = pd.concat(rows, ignore_index=True).dropna(subset=["oraclePx", "bid1_px", "ask1_px"])
    df["time"] = pd.to_datetime(df.recv_ms, unit="ms", utc=True)
    # segments: a new one after every recorder gap
    df = df.sort_values(["coin", "recv_ms"]).reset_index(drop=True)
    df["seg"] = (df.groupby("coin").stale.transform(lambda s: s.astype(int).diff().fillna(1).ne(0))
                 | (df.coin != df.coin.shift())).cumsum()
    df = df[~df.stale]
    # aggressive notional by OTHER accounts in the last SWEEP_S seconds, per side
    tr = tr[(tr.buyer.str.lower() != TEAM) & (tr.seller.str.lower() != TEAM)]
    tr = tr.assign(usd=tr.px * tr.sz, c=tr.coin.str.split(":").str[1])
    for side, col in (("B", "sweep_buy"), ("A", "sweep_sell")):
        df[col] = 0.0
        for coin, g in df.groupby("coin"):
            t = tr[(tr.c == coin) & (tr.side == side)].sort_values("recv_ms")
            cum = np.concatenate([[0.0], t.usd.cumsum().to_numpy()])
            ts = t.recv_ms.to_numpy()
            hi = np.searchsorted(ts, g.recv_ms.to_numpy(), side="right")
            lo = np.searchsorted(ts, g.recv_ms.to_numpy() - SWEEP_S * 1000, side="right")
            df.loc[g.index, col] = cum[hi] - cum[lo]
    return df


def add_ml(df, shadow_folder):
    files = sorted(Path(shadow_folder).expanduser().glob("shadow_*.csv"))
    if not files:
        df["pred"] = np.nan
        return df
    sh = pd.concat([pd.read_csv(f, usecols=["t", "coin", "pred_gbm_bp"]) for f in files], ignore_index=True)
    sh = sh[sh.t >= START.timestamp()].drop_duplicates(["coin", "t"]).set_index(["coin", "t"]).pred_gbm_bp
    idx = pd.MultiIndex.from_arrays([df.coin, df.recv_ms // 1000])
    df["pred"] = sh.reindex(idx).to_numpy()
    return df


def with_basis(g, hours=6.0, warm_min=30):
    """E47: reference = oracle x (1 + rolling median of the mid's premium over
    the last `hours`); rows with < warm_min minutes of history can't enter."""
    prem = ((g.bid1_px + g.ask1_px) / 2 / g.oraclePx - 1) * 1e4
    win = int(hours * 3600 / CHECK_S)
    med = prem.rolling(win, min_periods=1).median()
    warm = np.arange(len(g)) >= warm_min * 60 / CHECK_S
    return g.assign(oraclePx=g.oraclePx * (1 + med / 1e4)), ~warm


def run(df, entry, ml, sweep, basis=False):
    out = []
    for coin, g in df.groupby("coin"):
        g = g.reset_index(drop=True)
        cold = None
        if basis:
            g, cold = with_basis(g)
        buy_px, sell_px = fill_prices(g, NOTIONAL)
        kw = {}
        if ml:
            kw["pred"] = g.pred.to_numpy()
        if sweep:
            kw["block_sell"] = (g.sweep_buy >= sweep).to_numpy()
            kw["block_buy"] = (g.sweep_sell >= sweep).to_numpy()
        if cold is not None:
            kw["block_sell"] = kw.get("block_sell", np.zeros(len(g), bool)) | cold
            kw["block_buy"] = kw.get("block_buy", np.zeros(len(g), bool)) | cold
        for t in simulate_v2(g, buy_px, sell_px, entry_edge_bp=entry, **BASE, **kw):
            out.append({"coin": coin} | t)
    return pd.DataFrame(out)


def summ(tr):
    if tr.empty:
        return {"trades": 0, "net": 0.0, "excl_oracle": 0.0, "win_%": np.nan, "oracle_move": 0.0}
    return {"trades": len(tr), "net": tr.net.sum(), "excl_oracle": (tr.net - tr.oracle_move).sum(),
            "win_%": 100 * (tr.net > 0).mean(), "oracle_move": tr.oracle_move.sum()}


def main():
    folder, shadow = sys.argv[1], sys.argv[2]
    pd.set_option("display.width", 220)
    pd.set_option("display.float_format", "{:.1f}".format)
    df = add_ml(load(folder), shadow)
    print(f"pas checks every {CHECK_S} s: {df.time.min():%m-%d %H:%M} -> {df.time.max():%m-%d %H:%M} UTC, "
          f"{df.coin.nunique()} markets, ML prediction available on {100 * df.pred.notna().mean():.0f}% of checks")
    edge = []
    for coin, g in df.groupby("coin"):
        b, s = fill_prices(g, NOTIONAL)
        edge.append(pd.DataFrame({"coin": coin, "time": g.time.to_numpy(),
                                  "edge": np.fmax((s / g.oraclePx - 1) * 1e4, (1 - b / g.oraclePx) * 1e4)}))
    edge = pd.concat(edge)
    print("\nShare of checks with a $25k fill at least X bp better than the oracle:")
    print(pd.DataFrame({f">={x} bp": edge.groupby("coin").edge.apply(lambda e: 100 * (e >= x).mean())
                        for x in (20, 25, 30, 40)}).round(2).to_string())

    rows, trades = [], {}
    for entry, ml, sweep in GRID:
        tr = run(df, entry, ml, sweep)
        trades[(entry, ml, sweep)] = tr
        d = tr[pd.to_datetime(tr.t0) < SPLIT] if len(tr) else tr
        t = tr[pd.to_datetime(tr.t0) >= SPLIT] if len(tr) else tr
        rows.append({"entry_bp": entry, "ml": ml, "sweep_$": sweep or 0,
                     **{f"D_{k}": v for k, v in summ(d).items()}, **{f"T_{k}": v for k, v in summ(t).items()}})
    res = pd.DataFrame(rows)
    print(f"\nD_ = DESIGN (to {SPLIT:%m-%d %H:%M} UTC), T_ = TEST (after). $ per setting, $25k per trade:")
    print(res.to_string(index=False))

    ok = res[res.D_trades >= 5]
    live = res[(res.entry_bp == LIVE[0]) & (res.ml == LIVE[1]) & (res["sweep_$"] == 0)]
    if ok.empty:
        print("\nLOCKED RULE: no setting has >= 5 design trades -> keep the live setting.")
    else:
        best = ok.loc[ok.D_net.idxmax()]
        lt = float(live.T_net.iloc[0])
        adopt = best.T_net > 0 and best.T_net >= lt
        print(f"\nLOCKED RULE: design pick = entry {best.entry_bp} bp, ML {best.ml}, sweep ${best['sweep_$']:,.0f} "
              f"(design net ${best.D_net:,.0f}); test net ${best.T_net:,.0f} vs live setting ${lt:,.0f} -> "
              f"{'ADOPT' if adopt else 'KEEP the live setting'}")
    print("\nE47: live setting (30 bp, ML on) on ALL pas data, oracle vs basis-adjusted reference:")
    rows = []
    for basis in (False, True):
        for entry in (20, 30):
            tr_b = run(df, entry, True, None, basis=basis)
            rows.append({"reference": "usual level" if basis else "oracle", "entry_bp": entry, **summ(tr_b)})
            if basis and entry == 30 and len(tr_b):
                print(tr_b.assign(t0=pd.to_datetime(tr_b.t0).dt.strftime("%m-%d %H:%M"))[
                    ["coin", "t0", "side", "net", "why"]].to_string(index=False))
    print(pd.DataFrame(rows).to_string(index=False))
    tr = trades[LIVE]
    if len(tr):
        print("\nLive setting's simulated trades:")
        tr = tr.assign(t0=pd.to_datetime(tr.t0).dt.strftime("%m-%d %H:%M:%S"))
        print(tr[["coin", "t0", "side", "edge_in", "edge_out", "oracle_move", "fees", "net", "why"]].to_string(index=False))


if __name__ == "__main__":
    main()
