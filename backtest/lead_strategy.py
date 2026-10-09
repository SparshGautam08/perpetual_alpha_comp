"""E21: trade on par when Hyperliquid mainnet jumps, before par's oracle
(our scoring price) and par's book catch up (E20: they lag by ~5-9 s).

    .venv/bin/python -m backtest.lead_strategy

Signal, per market on a 1 s grid:
  premium = (mainnet mid / par oracle - 1) in bp, minus its rolling
  30-minute median (removes persistent offsets, e.g. GOLD, EWJ).
  premium >= K: buy $N on par; premium <= -K: sell $N. Close after H s.
Execution against par's RECORDED book (data_ws/l2_*, full book on change):
  - optimistic: the last book seen at the signal second
  - conservative: the NEXT book update after the signal (book had more
    time to catch up). Real fills should fall between the two.
Taker fee FEE_BPS on entry and exit. Funding ignored (holds <= 5 min).
P&L is split into entry edge vs oracle, exit cost vs oracle, oracle move,
fees. Here the oracle move IS the predicted part.

Design data: up to DESIGN_END. Settings chosen here are locked; the test
uses data after DESIGN_END only.
"""
import itertools
from pathlib import Path

import numpy as np
import pandas as pd

from .costs import FEE_BPS
from .lead_lag import PAIRS, _read

ROOT = Path(__file__).resolve().parent.parent
DESIGN_END = pd.Timestamp("2026-10-09 23:09:00", tz="UTC")
LEVELS = 20
GRID = {"K_bp": [10, 15, 20, 30], "H_s": [30, 60, 300], "N_usd": [10_000, 25_000]}
BASELINE_S = 1800


def book_fills(l2: pd.DataFrame, usd: float):
    """Per l2 row: average buy and sell price for $usd walking all levels."""
    out = {}
    for side in ("ask", "bid"):
        left = np.full(len(l2), float(usd))
        units = np.zeros(len(l2))
        for i in range(1, LEVELS + 1):
            pxc, szc = f"{side}{i}_px", f"{side}{i}_sz"
            if pxc not in l2:
                break
            px = l2[pxc].to_numpy(dtype=float)
            avail = np.nan_to_num(px * l2[szc].to_numpy(dtype=float))
            take = np.minimum(left, avail)
            units += np.where(px > 0, take / np.where(px > 0, px, 1), 0)
            left -= take
        out[side] = np.where(left <= 1e-6, usd / np.maximum(units, 1e-12), np.nan)
    return out["ask"], out["bid"]


def load(end=None, start=None):
    """Per market: 1 s grid with par oracle, mainnet mid, premium; plus the
    par book (recv time, buy/sell fill price per size)."""
    par = _read("data_ws/ctxws_*.csv", ["recv_ms", "coin", "oraclePx"])
    mb = _read("data_mainnet/mn_bbo_*.csv", ["recv_ms", "coin", "bid_px", "ask_px"])
    l2_all = pd.concat([pd.read_csv(f) for f in sorted((ROOT / "server_data" / "data_ws").glob("l2_*.csv"))],
                       ignore_index=True)
    t0 = max(par.recv_ms.min(), mb.recv_ms.min())
    t1 = min(par.recv_ms.max(), mb.recv_ms.max(), l2_all.recv_ms.max())
    grid = pd.date_range(pd.Timestamp(t0, unit="ms", tz="UTC").ceil("s"),
                         pd.Timestamp(t1, unit="ms", tz="UTC").floor("s"), freq="s")
    ff = lambda df, col: (df.assign(t=pd.to_datetime(df.recv_ms, unit="ms", utc=True))
                          .drop_duplicates("t", keep="last").set_index("t")[col].sort_index()
                          .reindex(grid, method="ffill"))
    out = {}
    for coin, mn in PAIRS.items():
        p = par[par.coin == f"par:{coin}"]
        m = mb[mb.coin == mn].assign(mid=lambda d: (d.bid_px + d.ask_px) / 2)
        l2 = l2_all[l2_all.coin == f"par:{coin}"].sort_values("recv_ms").reset_index(drop=True)
        if p.empty or m.empty or l2.empty:
            continue
        g = pd.DataFrame({"oracle": ff(p, "oraclePx"), "m_mid": ff(m, "mid")}).dropna()
        raw = (g.m_mid / g.oracle - 1) * 1e4
        g["premium"] = raw - raw.rolling(BASELINE_S, min_periods=300).median()
        if start is not None:
            g = g[g.index > start]
        if end is not None:
            g = g[g.index <= end]
        fills = {n: book_fills(l2, n) for n in GRID["N_usd"]}
        out[coin] = {"g": g, "l2_t": l2.recv_ms.to_numpy(dtype="int64"),
                     "fills": fills}
    return out


def fill_at(d, n, t, side, conservative):
    """Fill price for side (+1 buy / -1 sell) at time t."""
    lt = d["l2_t"]
    i = np.searchsorted(lt, t.value // 1_000_000, side="right")   # ms since epoch
    i = i if conservative else i - 1        # next update vs last seen
    if i < 0 or i >= len(lt):
        return np.nan
    buy, sell = d["fills"][n]
    return buy[i] if side > 0 else sell[i]


def simulate(data, K, H, N, conservative) -> pd.DataFrame:
    rows = []
    for coin, d in data.items():
        g = d["g"]
        prem = g.premium.to_numpy()
        orc = g.oracle.to_numpy()
        idx = g.index
        i, n = 0, len(g)
        while i < n - H:
            p = prem[i]
            if np.isnan(p) or abs(p) < K:
                i += 1
                continue
            side = 1 if p > 0 else -1
            t_in, t_out = idx[i], idx[i + H]
            px_in = fill_at(d, N, t_in, side, conservative)
            px_out = fill_at(d, N, t_out, -side, conservative)
            if np.isnan(px_in) or np.isnan(px_out):
                i += 1
                continue
            q = N / px_in
            o_in, o_out = orc[i], orc[i + H]
            rows.append({"coin": coin, "t": t_in, "side": side, "premium": p,
                         "edge_in": side * q * (o_in - px_in),
                         "edge_out": side * q * (px_out - o_out),
                         "oracle_move": side * q * (o_out - o_in),
                         "fees": -FEE_BPS / 1e4 * q * (px_in + px_out)})
            i += H                                   # one position at a time per market
    t = pd.DataFrame(rows)
    if not t.empty:
        t["net"] = t.edge_in + t.edge_out + t.oracle_move + t.fees
    return t


def summarize(t: pd.DataFrame, N: float) -> dict:
    if t.empty:
        return {"trades": 0, "net_$": 0.0}
    return {"trades": len(t), "net_$": t.net.sum(), "bp_per_trade": t.net.mean() / N * 1e4,
            "win_%": 100 * (t.net > 0).mean(), "oracle_move_$": t.oracle_move.sum(),
            "edge_in_$": t.edge_in.sum(), "edge_out_$": t.edge_out.sum(), "fees_$": t.fees.sum()}


def main():
    pd.set_option("display.width", 230)
    pd.set_option("display.float_format", "{:.2f}".format)
    data = load(end=DESIGN_END)
    g0 = next(iter(data.values()))["g"]
    print(f"DESIGN data: {g0.index[0]:%m-%d %H:%M} -> {g0.index[-1]:%m-%d %H:%M} UTC, fee {FEE_BPS} bp/side\n")
    rows = []
    for K, H, N in itertools.product(*GRID.values()):
        for cons in (False, True):
            t = simulate(data, K, H, N, cons)
            rows.append({"K_bp": K, "H_s": H, "N_usd": N,
                         "fills": "conservative" if cons else "optimistic"} | summarize(t, N))
    r = pd.DataFrame(rows)
    piv = r.pivot_table(index=["K_bp", "H_s", "N_usd"], columns="fills",
                        values=["net_$", "trades", "bp_per_trade"]).round(1)
    print("=== DESIGN grid: net $ / trades / bp per trade, optimistic vs conservative fills ===")
    print(piv.to_string())
    r.to_csv(ROOT / "results" / "lead_grid_design.csv", index=False)

    best = r[r.fills == "conservative"].sort_values("net_$", ascending=False).iloc[0]
    print(f"\nBest conservative cell: K={best.K_bp} H={best.H_s} N={best.N_usd}")
    t = simulate(data, int(best.K_bp), int(best.H_s), int(best.N_usd), True)
    print(t.groupby("coin")[["net", "oracle_move", "edge_in", "edge_out", "fees"]]
          .agg(["sum"]).droplevel(1, axis=1).assign(trades=t.groupby("coin").size()).to_string())


if __name__ == "__main__":
    main()
