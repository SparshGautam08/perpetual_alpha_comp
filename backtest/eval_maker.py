"""Compare paper maker runs by fair-price variant (bot/maker.py --fair gbm vs
--fair base): fills, edge at fill, and markouts after 10 / 60 / 300 s.

    .venv/bin/python -m backtest.eval_maker [folder]

The folder holds maker_paper_<fair>_*.csv (bot/state on the bot VM) and,
optionally, shadow_*.csv. Markouts are recomputed from the shadow log's
oracle (one row per market per second) when it is there, so fills whose
markout the maker never logged (process restarted) still count; otherwise
the maker's own markout rows are used. Only the time window covered by
every variant is compared. Markout = side x (oracle at t+h - fill px) / px
in bp, before the 3 bp maker fee; "net" columns subtract the fee.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HS = (10, 60, 300)
FEE_BP = 3.0


def load_fills(folder):
    rows = []
    for f in sorted(folder.glob("maker_paper_*_*.csv")):
        fair = f.name.split("_")[2]
        d = pd.read_csv(f, dtype={"detail": str})
        fl = d[d.event == "fill"].copy()
        if fl.empty:        # e.g. a false start that only quoted
            continue
        fl["fair"] = fair
        fl["edge_bp"] = pd.to_numeric(fl.detail.str.extract(r"edge_bp=(\S+)")[0], errors="coerce")
        fl["through"] = fl.detail.str.extract(r"through=(\S+)")[0] == "True"
        fl["t_fill"] = fl.t_ms // 1000
        # the maker's own markouts, matched to the fill by coin/side/px/sz and time
        for h in HS:
            mo = d[d.event == f"markout_{h}s"].copy()
            mo["t_fill"] = mo.t_ms // 1000 - h
            fl = fl.merge(mo[["t_fill", "coin", "side", "px", "sz", "detail"]]
                          .rename(columns={"detail": f"log_mo_{h}s"}),
                          on=["t_fill", "coin", "side", "px", "sz"], how="left")
        rows.append(fl)
    if not rows:
        sys.exit(f"no maker fills in {folder}")
    return pd.concat(rows, ignore_index=True)


def add_markouts(fl, folder):
    files = sorted(folder.glob("shadow_*.csv"))
    if not files:
        print("No shadow_*.csv: using the maker's own logged markouts.\n")
        for h in HS:
            fl[f"mo_{h}s"] = pd.to_numeric(fl[f"log_mo_{h}s"], errors="coerce")
        return fl
    sh = pd.concat([pd.read_csv(f, usecols=["t", "coin", "oracle"]) for f in files])
    sh = sh.drop_duplicates(["coin", "t"]).set_index(["coin", "t"]).oracle
    sign = np.where(fl.side == "buy", 1.0, -1.0)
    for h in HS:
        idx = pd.MultiIndex.from_arrays([fl.coin, fl.t_fill + h])
        o = sh.reindex(idx).to_numpy()
        fl[f"mo_{h}s"] = sign * (o - fl.px) / fl.px * 1e4
    print(f"Markouts from the shadow oracle ({len(files)} file(s)).\n")
    return fl


def summarize(g):
    out = {"fills": len(g), "buys": int((g.side == "buy").sum()),
           "volume_$": (g.px * g.sz).sum(), "through_%": 100 * g.through.mean(),
           "edge_bp": g.edge_bp.mean()}
    for h in HS:
        m = g[f"mo_{h}s"].dropna()
        out[f"mo_{h}s"] = m.mean()
        out[f"se_{h}s"] = m.std(ddof=1) / np.sqrt(len(m)) if len(m) > 1 else np.nan
    out["net_300s"] = out["mo_300s"] - FEE_BP
    # paper P&L if every fill were marked to the oracle 300 s later, after fees
    out["pnl_300s_$"] = ((g.px * g.sz) * (g["mo_300s"] - FEE_BP) / 1e4).sum()
    return pd.Series(out)


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "server_data"
    fl = load_fills(folder)
    span = fl.groupby("fair").t_ms.agg(["min", "max"])
    t0, t1 = span["min"].max(), span["max"].min()
    for fair, r in span.iterrows():
        print(f"{fair:5s} fills {pd.to_datetime(r['min'], unit='ms'):%m-%d %H:%M} -> "
              f"{pd.to_datetime(r['max'], unit='ms'):%m-%d %H:%M} UTC")
    if t1 > t0:
        fl = fl[(fl.t_ms >= t0) & (fl.t_ms <= t1)]
        print(f"Common window: {pd.to_datetime(t0, unit='ms'):%m-%d %H:%M} -> "
              f"{pd.to_datetime(t1, unit='ms'):%m-%d %H:%M} UTC ({(t1 - t0) / 3.6e6:.1f} h)\n")
    fl = add_markouts(fl, folder)
    pd.set_option("display.float_format", "{:.2f}".format)
    pd.set_option("display.width", 200)
    print("BY VARIANT (bp; markouts before the 3 bp fee, net_300s after it)")
    print(fl.groupby("fair").apply(summarize).to_string())
    print("\nBY VARIANT AND MARKET")
    print(fl.groupby(["fair", "coin"]).apply(summarize)[
        ["fills", "buys", "edge_bp", "mo_10s", "mo_60s", "mo_300s", "net_300s", "pnl_300s_$"]].to_string())
    print("\n'THROUGH' FILLS ONLY (trade beyond our price: the conservative fill model)")
    print(fl[fl.through].groupby("fair").apply(summarize)[
        ["fills", "edge_bp", "mo_10s", "mo_60s", "mo_300s", "net_300s", "pnl_300s_$"]].to_string())


if __name__ == "__main__":
    main()
