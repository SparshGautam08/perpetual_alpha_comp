"""E20: does Hyperliquid mainnet lead par's oracle and par's book?

    .venv/bin/python -m backtest.lead_lag

Data (both recorded by record_ws.py on the same VM, so recv_ms clocks match):
- par:     server_data/data_ws/ctxws_*.csv (oracle, mark, mid; ~1 s, on change)
- mainnet: server_data/data_mainnet/mn_bbo_*.csv (best bid/ask on every
           price change) and mn_ctxws_*.csv (mainnet oracle/mid ~1 s)
Everything is put on a 1-second grid (last value, forward-filled) over the
period where both are recorded.

Measures, per market:
1. Lead/lag: correlation of 1 s returns of par's oracle (and par's mid)
   with mainnet mid returns shifted by -30..+30 s. Positive best lag =
   mainnet moves FIRST by that many seconds.
2. Prediction: premium = mainnet mid / par oracle - 1 (bp) now, vs par
   oracle change over the next H seconds.
3. Drift link: premium at the drift v2 entries (E19).
"""
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SD = ROOT / "server_data"
PAIRS = {"BTC": "BTC", "ETH": "ETH", "HYPE": "HYPE", "SP500": "xyz:SP500",
         "EWY": "xyz:EWY", "EWJ": "xyz:EWJ", "BRENT": "xyz:BRENTOIL",
         "GOLD": "xyz:GOLD", "USDEUR": "xyz:EUR", "USDJPY": "xyz:JPY"}
LAGS = range(-30, 31)
HORIZONS = [5, 15, 30, 60]


def _read(pattern, cols):
    files = sorted(SD.glob(pattern))
    return pd.concat([pd.read_csv(f, usecols=cols) for f in files], ignore_index=True)


def load_grid() -> dict[str, pd.DataFrame]:
    par = _read("data_ws/ctxws_*.csv", ["recv_ms", "coin", "oraclePx", "midPx"])
    mbbo = _read("data_mainnet/mn_bbo_*.csv", ["recv_ms", "coin", "bid_px", "ask_px"])
    mctx = _read("data_mainnet/mn_ctxws_*.csv", ["recv_ms", "coin", "oraclePx"])
    t0 = max(par.recv_ms.min(), mbbo.recv_ms.min())
    t1 = min(par.recv_ms.max(), mbbo.recv_ms.max())
    grid = pd.date_range(pd.Timestamp(t0, unit="ms", tz="UTC").ceil("s"),
                         pd.Timestamp(t1, unit="ms", tz="UTC").floor("s"), freq="s")

    def on_grid(df, col):
        s = df.assign(t=pd.to_datetime(df.recv_ms, unit="ms", utc=True)).set_index("t")[col]
        s = s[~s.index.duplicated(keep="last")].sort_index()
        return s.reindex(grid, method="ffill")

    out = {}
    for coin, mn in PAIRS.items():
        p = par[par.coin == f"par:{coin}"]
        b = mbbo[mbbo.coin == mn].assign(mid=lambda d: (d.bid_px + d.ask_px) / 2)
        m = mctx[mctx.coin == mn]
        if p.empty or b.empty:
            continue
        out[coin] = pd.DataFrame({"p_oracle": on_grid(p, "oraclePx"), "p_mid": on_grid(p, "midPx"),
                                  "m_mid": on_grid(b, "mid"), "m_oracle": on_grid(m, "oraclePx")}).dropna()
    return out


def lead_lag(g: pd.DataFrame, target: str) -> tuple[int, float, float]:
    """Best lag (s) of mainnet-mid returns vs target returns, the correlation
    there, and the same-second correlation."""
    m = np.log(g.m_mid).diff() * 1e4
    y = np.log(g[target]).diff() * 1e4
    corr = {k: y.corr(m.shift(k)) for k in LAGS}     # k > 0: mainnet k seconds earlier
    best = max(corr, key=lambda k: corr[k] if pd.notna(corr[k]) else -9)
    return best, corr[best], corr[0]


def prediction(g: pd.DataFrame) -> dict:
    prem = (g.m_mid / g.p_oracle - 1) * 1e4
    out = {"prem_med_abs_bp": prem.abs().median()}
    for h in HORIZONS:
        fwd = (g.p_oracle.shift(-h) / g.p_oracle - 1) * 1e4
        ok = prem.notna() & fwd.notna()
        out[f"corr_{h}s"] = prem[ok].corr(fwd[ok])
        big = ok & (prem.abs() >= 3)                   # mainnet >= 3 bp away
        moved = big & (fwd != 0)
        out[f"hit_{h}s_%"] = 100 * (np.sign(prem[moved]) == np.sign(fwd[moved])).mean() if moved.any() else np.nan
    out["n_prem>=3bp"] = int((prem.abs() >= 3).sum())
    return out


def main():
    pd.set_option("display.width", 220)
    pd.set_option("display.float_format", "{:.2f}".format)
    grids = load_grid()
    any_g = next(iter(grids.values()))
    print(f"Overlap: {any_g.index[0]:%m-%d %H:%M} -> {any_g.index[-1]:%m-%d %H:%M} UTC, "
          f"{len(any_g):,} seconds\n")

    rows = {}
    for coin, g in grids.items():
        lo, co, c0 = lead_lag(g, "p_oracle")
        lm, cm, cm0 = lead_lag(g, "p_mid")
        moves = (g.p_oracle.diff() != 0).mean() * 100
        rows[coin] = {"oracle_best_lag_s": lo, "oracle_corr@best": co, "oracle_corr@0": c0,
                      "parmid_best_lag_s": lm, "parmid_corr@best": cm,
                      "oracle_changes_%_of_s": moves} | prediction(g)
    t = pd.DataFrame(rows).T
    print("=== Lead/lag: best lag > 0 means mainnet moves first by that many seconds ===")
    print(t[["oracle_best_lag_s", "oracle_corr@best", "oracle_corr@0", "parmid_best_lag_s",
             "parmid_corr@best", "oracle_changes_%_of_s"]].to_string())
    print("\n=== Prediction: mainnet premium over par oracle now vs par oracle change next H s ===")
    print(t[["prem_med_abs_bp", "n_prem>=3bp"] + [f"corr_{h}s" for h in HORIZONS]
            + [f"hit_{h}s_%" for h in HORIZONS]].to_string())
    t.to_csv(ROOT / "results" / "lead_lag.csv")

    # Drift v2 test entries (E19): premium at entry vs what the oracle did.
    entries = [("ETH", "2026-10-09 10:36"), ("ETH", "2026-10-09 11:02"), ("ETH", "2026-10-09 11:06"),
               ("BTC", "2026-10-09 12:53"), ("ETH", "2026-10-09 13:33"), ("ETH", "2026-10-09 14:42"),
               ("HYPE", "2026-10-09 18:35"), ("BRENT", "2026-10-09 18:50"), ("BTC", "2026-10-09 19:50")]
    print("\n=== Drift v2 test entries (all sells): mainnet premium at entry ===")
    for coin, ts in entries:
        g = grids[coin]
        t0 = g.index[g.index.get_indexer([pd.Timestamp(ts, tz="UTC")], method="nearest")[0]]
        prem = (g.m_mid[t0] / g.p_oracle[t0] - 1) * 1e4
        o30 = (g.p_oracle.asof(t0 + pd.Timedelta(minutes=30)) / g.p_oracle[t0] - 1) * 1e4
        print(f"{coin:6s} {ts}  mainnet vs par oracle {prem:+6.1f} bp   par oracle next 30 min {o30:+6.1f} bp")


if __name__ == "__main__":
    main()
