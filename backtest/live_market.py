"""Step 8: live par market microstructure from our recorder.

    .venv/bin/python -m backtest.live_market

Reads server_data/ctx_*.csv (oracle, mark, funding) and book_*.csv (top 5
levels), sampled every 5 s. Per market it measures:
- spread: best ask vs best bid, in bp of mid
- depth: $ notional at the best level and across the top 5 levels, and
  the cost of a market order of a given size (walking the book)
- drift: how far mark price and book mid sit from the oracle
- catch-up: after the oracle jumps, how long until the book mid / mark
  move half of the way; and how often the quotes update at all
- funding: average, range, annualized
and ranks markets by round-trip trading cost measured against the ORACLE
(what we are scored on), including the fee from costs.py.

Recorder gaps (CLAUDE.md) are respected: no difference or lag is computed
across a step longer than MAX_STEP_S.

Analysis settings are fixed in PARAMS below and were chosen on Oct 7 2026,
before any later data was seen. Do not tune them to new data; instead,
`sensitivity()` reruns the key results over a grid of alternatives so the
report can say how much each choice matters.
"""
from pathlib import Path

import numpy as np
import pandas as pd

from .costs import FEE_BPS

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "server_data"
ORDER_SIZES = [10_000, 50_000, 100_000, 250_000]   # $ notional

# Fixed analysis settings (chosen Oct 7 2026, before seeing later data).
MAX_STEP_S = 15          # longer steps count as a recorder gap
JUMP_BP = 3.0            # an oracle "jump" is a 5s move of at least this...
JUMP_PCTL = 0.95         # ...and in the market's top 5% of 5s moves
CATCHUP_FRAC = 0.5       # "caught up" = moved this share of the jump
CATCHUP_MAX = 120        # follow each jump for 120 steps (10 min)
US_HOURS = (13, 20)      # 13:30-20:00 UTC NYSE session, rounded to hours

# Alternatives tried by sensitivity().
SENS_JUMPS = [(2.0, 0.90), (3.0, 0.95), (5.0, 0.95), (3.0, 0.99)]
SENS_FRACS = [0.5, 0.75, 1.0]
SENS_US_HOURS = [(13, 20), (14, 20), (12, 21)]


def load() -> pd.DataFrame:
    ctx = pd.concat([pd.read_csv(f) for f in sorted(SRC.glob("ctx_*.csv"))])
    book = pd.concat([pd.read_csv(f) for f in sorted(SRC.glob("book_*.csv"))])
    df = ctx.merge(book, on=["ts_ms", "coin"], how="inner")
    df["coin"] = df["coin"].str.replace("par:", "", regex=False)
    df["time"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    df = df.sort_values(["coin", "ts_ms"]).reset_index(drop=True)
    df["mid"] = (df.bid1_px + df.ask1_px) / 2
    # new_seg marks the first row after a gap (or a new coin).
    step = df.groupby("coin")["ts_ms"].diff() / 1000
    df["new_seg"] = step.isna() | (step > MAX_STEP_S)
    df["seg"] = df["new_seg"].cumsum()
    return df


def walk_book(row, side: str, usd: float):
    """Average fill price for a market order of `usd` notional, walking
    the 5 recorded levels. NaN if the 5 levels can't fill it."""
    left, cost = usd, 0.0
    for i in range(1, 6):
        px, sz = row[f"{side}{i}_px"], row[f"{side}{i}_sz"]
        if np.isnan(px):
            break
        take = min(left, px * sz)
        cost += take / px          # units bought/sold
        left -= take
        if left <= 1e-9:
            return usd / cost
    return np.nan


def impact_costs(g: pd.DataFrame, n_samples: int = 2000) -> dict:
    """Round-trip cost vs oracle for each order size (bp), from a sample
    of snapshots: buy at the ask-side average price, sell at the bid side."""
    s = g.sample(min(n_samples, len(g)), random_state=0)
    out = {}
    for usd in ORDER_SIZES:
        buy = s.apply(lambda r: walk_book(r, "ask", usd), axis=1)
        sell = s.apply(lambda r: walk_book(r, "bid", usd), axis=1)
        ok = buy.notna() & sell.notna()
        rt = ((buy / s.oraclePx - 1) + (1 - sell / s.oraclePx))[ok] * 1e4
        out[f"rt_{usd // 1000}k_bp"] = rt.median() + 2 * FEE_BPS if ok.any() else np.nan
        out[f"fill_{usd // 1000}k_%"] = 100 * ok.mean()
        if usd == 50_000:   # one-way, vs oracle: drift makes one side cheaper
            out["buy_50k_bp"] = ((buy / s.oraclePx - 1) * 1e4).median() + FEE_BPS
            out["sell_50k_bp"] = ((1 - sell / s.oraclePx) * 1e4).median() + FEE_BPS
    return out


def catch_up(g: pd.DataFrame, col: str, jump_bp=None, jump_pctl=None,
             frac=None) -> dict:
    """After each oracle jump, steps until `col` has moved >= frac of the
    jump in the same direction (within the same recorder segment).
    Settings default to PARAMS above."""
    jump_bp = JUMP_BP if jump_bp is None else jump_bp
    jump_pctl = JUMP_PCTL if jump_pctl is None else jump_pctl
    frac = CATCHUP_FRAC if frac is None else frac
    lo = np.log(g.oraclePx.to_numpy())
    lx = np.log(g[col].to_numpy())
    seg = g.seg.to_numpy()
    d = np.diff(lo, prepend=np.nan) * 1e4
    d[g.new_seg.to_numpy()] = np.nan
    thr = max(jump_bp, np.nanquantile(np.abs(d), jump_pctl))
    lags = []
    for t in np.where(np.abs(d) >= thr)[0]:
        if t == 0:
            continue
        target = frac * (lo[t] - lo[t - 1])
        lag = np.nan
        for k in range(0, CATCHUP_MAX + 1):
            u = t + k
            if u >= len(lx) or seg[u] != seg[t]:
                break
            if np.sign(target) * (lx[u] - lx[t - 1]) >= abs(target):
                lag = k
                break
        lags.append(lag)
    lags = np.array(lags, dtype=float)
    caught = ~np.isnan(lags)
    return {"jumps": len(lags), "jump_thr_bp": thr,
            "median_s": 5 * np.median(lags[caught]) if caught.any() else np.nan,
            "caught_10min_%": 100 * caught.mean() if len(lags) else np.nan}


def per_market(g: pd.DataFrame) -> dict:
    spread = (g.ask1_px - g.bid1_px) / g["mid"] * 1e4
    us = g.time.dt.hour.between(US_HOURS[0], US_HOURS[1] - 1)
    top_usd = (g.bid1_px * g.bid1_sz + g.ask1_px * g.ask1_sz) / 2
    top5_bid = sum(g[f"bid{i}_px"] * g[f"bid{i}_sz"] for i in range(1, 6))
    top5_ask = sum(g[f"ask{i}_px"] * g[f"ask{i}_sz"] for i in range(1, 6))
    mark_d = (g.markPx / g.oraclePx - 1) * 1e4
    mid_d = (g["mid"] / g.oraclePx - 1) * 1e4
    within = ~g.new_seg
    o_chg = (g.oraclePx.diff() != 0)[within].mean()
    q_chg = ((g.bid1_px.diff() != 0) | (g.ask1_px.diff() != 0))[within].mean()
    hourly = pd.DataFrame({"f": g.funding, "d": mid_d}).groupby(g.time.dt.floor("h")).mean()
    return {
        "spread_mean_bp": spread.mean(),
        "spread_med_bp": spread.median(),
        "spread_p90_bp": spread.quantile(0.9),
        "spread_US_hrs_bp": spread[us].mean(),
        "spread_off_hrs_bp": spread[~us].mean(),
        "top_level_$k": top_usd.median() / 1e3,
        "top5_bid_$k": top5_bid.median() / 1e3,
        "top5_ask_$k": top5_ask.median() / 1e3,
        "orders_at_top": g.bid1_n.median(),
        "mark_vs_oracle_bp": mark_d.mean(),
        "mark_absdev_bp": mark_d.abs().mean(),
        "mid_vs_oracle_bp": mid_d.mean(),
        "mid_absdev_bp": mid_d.abs().mean(),
        "mid_absdev_p95_bp": mid_d.abs().quantile(0.95),
        "oracle_updates_%": 100 * o_chg,
        "quote_updates_%": 100 * q_chg,
        "ask<oracle_%": 100 * (g.ask1_px < g.oraclePx).mean(),
        "bid>oracle_%": 100 * (g.bid1_px > g.oraclePx).mean(),
        "corr_funding_drift_1h": hourly.corr().iloc[0, 1],
        "funding_apr_%": 100 * g.funding.mean() * 24 * 365,
        "funding_min_apr_%": 100 * g.funding.min() * 24 * 365,
        "funding_max_apr_%": 100 * g.funding.max() * 24 * 365,
    }


def sensitivity(df: pd.DataFrame) -> None:
    """Rerun the threshold-dependent results over SENS_* alternatives."""
    rows = []
    for coin, g in df.groupby("coin"):
        g = g.reset_index(drop=True)
        for jb, pc in SENS_JUMPS:
            for fr in SENS_FRACS:
                r = catch_up(g, "mid", jb, pc, fr)
                rows.append({"coin": coin, "jump": f">={jb:g}bp & top {100 * (1 - pc):.0f}%",
                             "need": f"{fr:.0%}", "jumps": r["jumps"],
                             "median_s": r["median_s"], "caught_%": r["caught_10min_%"]})
    t = pd.DataFrame(rows)
    t = t[t.groupby("coin").jumps.transform("min") >= 20]   # skip markets with too few jumps
    default = (f">={JUMP_BP:g}bp & top {100 * (1 - JUMP_PCTL):.0f}%", f"{CATCHUP_FRAC:.0%}")
    print("\n=== Sensitivity: mid catch-up median seconds (markets with >= 20 jumps) ===")
    print(f"(fixed setting: {default[0]}, need {default[1]})")
    print(t.pivot_table(index=["jump", "need"], columns="coin", values="median_s").to_string())
    print("\n=== Sensitivity: % of jumps caught within 10 min ===")
    print(t.pivot_table(index=["jump", "need"], columns="coin", values="caught_%").to_string())

    rows = []
    for coin, g in df.groupby("coin"):
        spread = (g.ask1_px - g.bid1_px) / g["mid"] * 1e4
        for a, b in SENS_US_HOURS:
            us = g.time.dt.hour.between(a, b - 1)
            rows.append({"coin": coin, "US_hours_utc": f"{a}-{b}",
                         "US_minus_off_bp": spread[us].mean() - spread[~us].mean()})
    print("\n=== Sensitivity: spread in US hours minus off hours (bp), by window ===")
    print(pd.DataFrame(rows).pivot_table(index="coin", columns="US_hours_utc",
                                         values="US_minus_off_bp").to_string())


def main():
    pd.set_option("display.width", 240)
    pd.set_option("display.float_format", "{:.2f}".format)
    df = load()
    snaps = df.drop_duplicates("ts_ms")
    print(f"Snapshots: {len(snaps)} per market, {df.time.min():%Y-%m-%d %H:%M} -> "
          f"{df.time.max():%Y-%m-%d %H:%M} UTC, {df.seg.nunique() // df.coin.nunique()} "
          f"segment(s) per market after gaps > {MAX_STEP_S}s")
    hours = (snaps.ts_ms.diff().clip(upper=MAX_STEP_S * 1000).sum()) / 3.6e6
    print(f"Covered time excluding gaps: {hours:.1f} h\n")

    rows, cost_rows, cu_rows = {}, {}, {}
    for coin, g in df.groupby("coin"):
        g = g.reset_index(drop=True)
        rows[coin] = per_market(g)
        cost_rows[coin] = impact_costs(g)
        mid_cu, mark_cu = catch_up(g, "mid"), catch_up(g, "markPx")
        cu_rows[coin] = {"jumps": mid_cu["jumps"], "jump_thr_bp": mid_cu["jump_thr_bp"],
                         "mid_median_s": mid_cu["median_s"],
                         "mid_caught_10min_%": mid_cu["caught_10min_%"],
                         "mark_median_s": mark_cu["median_s"],
                         "mark_caught_10min_%": mark_cu["caught_10min_%"]}
    m = pd.DataFrame(rows).T
    c = pd.DataFrame(cost_rows).T
    cu = pd.DataFrame(cu_rows).T

    print("=== Spread (bp of mid) ===")
    print(m[["spread_mean_bp", "spread_med_bp", "spread_p90_bp",
             "spread_US_hrs_bp", "spread_off_hrs_bp"]].to_string())
    print("\n=== Depth (median $ notional) ===")
    print(m[["top_level_$k", "top5_bid_$k", "top5_ask_$k", "orders_at_top"]].to_string())
    print("\n=== Drift from oracle (bp; + = above oracle) ===")
    print(m[["mark_vs_oracle_bp", "mark_absdev_bp", "mid_vs_oracle_bp",
             "mid_absdev_bp", "mid_absdev_p95_bp"]].to_string())
    print("\n=== Update speed (share of 5s steps with a change) ===")
    print(m[["oracle_updates_%", "quote_updates_%"]].to_string())
    print("\n=== Quotes on the wrong side of the oracle (share of snapshots) ===")
    print(m[["ask<oracle_%", "bid>oracle_%"]].to_string())
    print("\n=== Catch-up after oracle jumps (time for 50% of the move) ===")
    print(cu.to_string())
    print("\n=== Funding (annualized %, + = longs pay) ===")
    print(m[["funding_apr_%", "funding_min_apr_%", "funding_max_apr_%",
             "corr_funding_drift_1h"]].to_string())

    rank = c.copy()
    rank["mid_absdev_bp"] = m["mid_absdev_bp"]
    rank["funding_apr_%"] = m["funding_apr_%"]
    # Round-trip costs are nearly identical (same bot ladder everywhere),
    # so ties are broken by how far the book wanders from the oracle.
    rank = rank.round(2)
    rank = rank.assign(_r=rank.rt_50k_bp.round(0)).sort_values(["_r", "mid_absdev_bp"]).drop(columns="_r")
    rank.insert(0, "rank", range(1, len(rank) + 1))
    print(f"\n=== Ranking by round-trip cost vs oracle, $50k market order "
          f"(spread + book impact + 2 x {FEE_BPS}bp fee), ties by drift ===")
    print(rank.to_string())

    sensitivity(df)

    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    pd.concat([m, c, cu], axis=1).to_csv(out / "live_market_stats.csv")


if __name__ == "__main__":
    main()
