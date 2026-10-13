"""E28: the first weekend (Oct 9-12): oracle freezes, reopen jumps, and
whether Hyperliquid mainnet's weekend moves predicted them. Exploratory.

    .venv/bin/python -m backtest.weekend_gap

Per market, on a 1 s grid from the websocket recordings (Fri 18:00 UTC to
Mon 18:00 UTC):
- the longest stretch with no oracle change (the weekend freeze), when it
  started and ended (CT = UTC-5);
- the jump when the oracle resumed: oracle 2 min after resuming vs the
  frozen value;
- mainnet's move over the same freeze (mainnet mid just before resuming vs
  at freeze start): did it point the same way as the jump, and how big?
- par's book mid vs the frozen oracle during the freeze (gaps drift can
  trade).
"""
import numpy as np
import pandas as pd

from .lead_lag import PAIRS, _read

START = pd.Timestamp("2026-10-09 18:00", tz="UTC")
END = pd.Timestamp("2026-10-12 18:00", tz="UTC")
CT = pd.Timedelta(hours=5)


def main():
    pd.set_option("display.width", 220)
    pd.set_option("display.float_format", "{:.1f}".format)
    par = _read("data_ws/ctxws_*.csv", ["recv_ms", "coin", "oraclePx", "midPx", "funding"])
    mb = _read("data_mainnet/mn_bbo_*.csv", ["recv_ms", "coin", "bid_px", "ask_px"])
    grid = pd.date_range(START, END, freq="s")
    ff = lambda df, col: (df.assign(t=pd.to_datetime(df.recv_ms, unit="ms", utc=True))
                          .drop_duplicates("t", keep="last").set_index("t")[col].sort_index()
                          .reindex(grid, method="ffill"))
    rows = []
    for coin, mn in PAIRS.items():
        p = par[par.coin == f"par:{coin}"]
        m = mb[mb.coin == mn].assign(mid=lambda d: (d.bid_px + d.ask_px) / 2)
        g = pd.DataFrame({"o": ff(p, "oraclePx"), "pm": ff(p, "midPx"), "f": ff(p, "funding"),
                          "m": ff(m, "mid")}).dropna()
        chg = g.o.diff().ne(0)
        run_id = chg.cumsum()
        runs = g.groupby(run_id).apply(lambda r: pd.Series({"t0": r.index[0], "t1": r.index[-1],
                                                            "secs": len(r)}))
        longest = runs.sort_values("secs").iloc[-1]
        t0, t1 = longest.t0, longest.t1
        frozen = g.o[t0]
        after = g.o.asof(t1 + pd.Timedelta(minutes=2))
        jump = (after / frozen - 1) * 1e4
        m_move = (g.m.asof(t1) / g.m[t0] - 1) * 1e4
        during = g.loc[t0:t1]
        gap = ((during.pm / during.o - 1) * 1e4).abs()
        rows.append({"market": coin, "freeze_h": longest.secs / 3600,
                     "frozen_from_CT": (t0 - CT).strftime("%a %H:%M"),
                     "resumed_CT": (t1 - CT).strftime("%a %H:%M"),
                     "reopen_jump_bp": jump, "mainnet_move_bp": m_move,
                     "same_sign": np.sign(jump) == np.sign(m_move) if abs(jump) > 1 else None,
                     "book_gap_med_bp": gap.median(), "book_gap_p95_bp": gap.quantile(0.95),
                     "funding_apr_%": during.f.mean() * 8760 * 100})
    t = pd.DataFrame(rows).set_index("market")
    print(f"Window {START - CT:%a %H:%M} -> {END - CT:%a %H:%M} CT; longest oracle freeze per market\n")
    print(t.to_string())
    big = t[t.freeze_h > 12]
    if len(big) > 1:
        print(f"\nMarkets frozen > 12 h: {len(big)}; corr(mainnet weekend move, reopen jump) = "
              f"{np.corrcoef(big.mainnet_move_bp, big.reopen_jump_bp)[0, 1]:.2f}")


if __name__ == "__main__":
    main()
