"""Summarize the historical dataset: coverage, data quality, and how
weekends / market closures show up.

    .venv/bin/python -m backtest.dataset_summary

The candles are Hyperliquid mainnet perp trades, which run 24/7 even when
the underlying market (NYSE, ICE, FX) is closed. So closures do not appear
as missing rows; they show up as hours with much lower volume and smaller
moves. The par oracle may instead freeze in those hours (to be confirmed
with our weekend recording).
"""
import numpy as np
import pandas as pd

from .data import MARKETS, load_market, load_panel

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
QUIET = 0.25   # an hour is "quiet" if its avg volume < 25% of the market's median hour


def quality(name: str) -> dict:
    raw = pd.read_csv(f"historical/{name}.csv", parse_dates=["time"])
    t = raw["time"]
    step = t.diff().dt.total_seconds().div(3600)
    return {
        "first_utc": t.iloc[0].strftime("%Y-%m-%d %H:%M"),
        "last_utc": t.iloc[-1].strftime("%Y-%m-%d %H:%M"),
        "rows": len(raw),
        "interval_h": step.mode().iloc[0],
        "missing_hours": int((step - 1).clip(lower=0).sum()),
        "dup_rows": int(t.duplicated().sum()),
        "zero_trade_rows": int((raw.trades == 0).sum()),
        "flat_hours_%": 100 * (raw.high == raw.low).mean(),
    }


def quiet_windows(df: pd.DataFrame) -> str:
    """Weekly hours (UTC) whose average volume is below QUIET x median,
    merged into ranges like 'Fri 21h-Sun 22h'."""
    v = df["volume"].fillna(0)
    prof = v.groupby([v.index.dayofweek, v.index.hour]).mean()
    prof = prof.reindex(pd.MultiIndex.from_product([range(7), range(24)]), fill_value=0)
    quiet = (prof < QUIET * prof.median()).to_numpy()   # 168 slots, Mon 00h first
    if not quiet.any():
        return "none"
    # Rotate so we start on a non-quiet slot, then find contiguous runs.
    start = int(np.argmin(quiet))
    runs, cur = [], None
    for k in range(168):
        i = (start + k) % 168
        if quiet[i] and cur is None:
            cur = i
        if not quiet[i] and cur is not None:
            runs.append((cur, (i - 1) % 168)); cur = None
    if cur is not None:
        runs.append((cur, (start - 1) % 168))
    fmt = lambda i: f"{DOW[i // 24]} {i % 24:02d}h"
    long_runs = [(a, b) for a, b in runs if (b - a) % 168 + 1 >= 3]   # drop 1-2h blips
    short = len(runs) - len(long_runs)
    out = ", ".join(f"{fmt(a)}-{fmt(b)}" for a, b in long_runs) or "only short dips"
    return out + (f" (+{short} short dips)" if short and long_runs else "")


def weekend_effects(df: pd.DataFrame) -> dict:
    """Compare weekend vs weekday activity, and the Fri-close -> Mon-open
    move vs an ordinary weekday overnight (21:00 -> 13:00 UTC next day)."""
    c = df["close"].ffill()
    r = np.log(c).diff()
    wk = df.index.dayofweek >= 5
    fri = c[(c.index.dayofweek == 4) & (c.index.hour == 20)]
    mon = c[(c.index.dayofweek == 0) & (c.index.hour == 13)]
    wkend = [abs(np.log(mon[mon.index > d].iloc[0] / p)) for d, p in fri.items()
             if (mon.index > d).any()]
    on = []
    for d in range(3):   # Mon->Tue, Tue->Wed, Wed->Thu overnights
        a = c[(c.index.dayofweek == d) & (c.index.hour == 20)]
        for t0, p in a.items():
            t1 = t0 + pd.Timedelta(hours=17)
            if t1 in c.index:
                on.append(abs(np.log(c[t1] / p)))
    return {
        "wkend_vol_vs_wkday": df.volume[wk].mean() / df.volume[~wk].mean(),
        "wkend_move_vs_wkday": r[wk].std() / r[~wk].std(),
        "fri20_to_mon13_absmove_%": 100 * np.median(wkend),
        "weekday_overnight_absmove_%": 100 * np.median(on),
    }


def main():
    pd.set_option("display.width", 220)
    pd.set_option("display.max_colwidth", 80)
    pd.set_option("display.float_format", "{:.2f}".format)
    print("Columns: time (UTC hour open), open, high, low, close, volume (asset units), trades\n")

    q = pd.DataFrame({m: quality(m) for m in MARKETS}).T
    print("=== Coverage and quality ===")
    print(q.to_string())

    print(f"\n=== When each market goes quiet (avg volume < {QUIET:.0%} of median hour, UTC) ===")
    for m in MARKETS:
        print(f"{m:7s} {quiet_windows(load_market(m))}")

    print("\n=== Weekend effects ===")
    w = pd.DataFrame({m: weekend_effects(load_market(m)) for m in MARKETS}).T
    print(w.to_string())

    _, ret, _, _ = load_panel(MARKETS)
    d = (1 + ret.loc["2026-04-18":]).resample("D").prod() - 1
    print("\n=== Daily return correlations (Apr 18 - Oct 5) ===")
    print(d.corr().round(2).to_string())


if __name__ == "__main__":
    main()
