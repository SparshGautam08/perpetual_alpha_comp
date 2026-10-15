"""What should risk parity's role be next to drift? (E38)

    .venv/bin/python -m backtest.rp_role

Drift: rule v2b as live (30 bp entry, 10 bp exit, 12 h, $25k, all 10
markets) on the recorded par data, checked every 15 s like the live bot.
Its daily P&L (by exit day, UTC) is the only drift history we have (~7
days), so 19-day competitions are simulated by resampling those days.

Risk parity: risk_parity_10v on CLEAN7 (the live bot's universe) on the
organizers' hourly history, Apr 18 -> Oct 5, with all costs; leverage L
scales it (live runs L = 0.25). Every 19-day window of that history is
one possible competition.

For each L: competition return and daily std (drift days + L x RP days,
drawn independently), and the score proxy (40 return + 30 low std, out
of 70) under the two guesses of the field's spread used in E5.
"""
import numpy as np
import pandas as pd

from .data import CLEAN, load_panel
from .drift_strategy import prepare, run_v2
from .engine import backtest, score
from .live_market import load
from .run_baselines import WARM_START, make_strategies

DAYS = 19                      # scored phase Oct 19 - Nov 6
EQUITY = 1_000_000
CHECK_S = 15
DRIFT = {"entry_edge_bp": 30, "exit_cost_bp": 10, "max_hold_h": 12, "need_funding": False,
         "stop_bp": 200}
LEVERAGES = [0, 0.1, 0.25, 0.5, 1.0]
DRIFT_SCALE = [1.0, 0.5]       # 0.5 = live catches only half the backtest's P&L
FIELDS = {"narrow": ((-5, 10), (0, 2)), "wide": ((-15, 30), (0, 4))}
DRAWS = 20
rng = np.random.default_rng(0)


def drift_daily():
    df = load()
    df = df.groupby(["coin", df.ts_ms // (CHECK_S * 1000)]).head(1)     # live check speed
    tr = run_v2(prepare(df), DRIFT)
    day = pd.to_datetime(tr.t1).dt.tz_localize(None).dt.floor("D") if pd.to_datetime(tr.t1).dt.tz is not None \
        else pd.to_datetime(tr.t1).dt.floor("D")
    first = df.time.min().tz_localize(None).ceil("D")
    last = df.time.max().tz_localize(None).floor("D")
    pnl = tr.net.groupby(day).sum().reindex(pd.date_range(first, last - pd.Timedelta(days=1)), fill_value=0.0)
    return tr, pnl


def rp_daily():
    _, ret, live, _ = load_panel()
    ret, live = ret[CLEAN], live[CLEAN]
    w, sched = make_strategies(ret, live)["risk_parity_10v"]
    eq = backtest(w, ret, sched, leverage=1.0, band=0.01).equity.loc[WARM_START:]
    return eq.resample("D").last().pct_change().dropna()


def main():
    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", "{:.2f}".format)
    tr, dd = drift_daily()
    rp = rp_daily()
    print(f"Drift v2b @ {CHECK_S} s: {len(tr)} trades, {len(dd)} full days "
          f"({dd.index[0]:%m-%d} -> {dd.index[-1]:%m-%d}); daily P&L ${dd.mean():,.0f} mean, "
          f"${dd.std():,.0f} std; days: " + ", ".join(f"{v:,.0f}" for v in dd))
    print(f"Risk parity 10% vol, CLEAN7, L=1: {len(rp)} days, daily mean {100 * rp.mean():+.3f}%, "
          f"std {100 * rp.std():.3f}%; per 19-day window at L=1: "
          f"median return {100 * ((1 + rp).rolling(DAYS).apply(np.prod, raw=True) - 1).median():+.2f}%\n")

    windows = [rp.iloc[i:i + DAYS].to_numpy() for i in range(len(rp) - DAYS + 1)]
    rows = []
    for ds in DRIFT_SCALE:
        for lev in LEVERAGES:
            sims = []
            for wnd in windows:
                for _ in range(DRAWS):
                    d = ds * rng.choice(dd.to_numpy(), DAYS) / EQUITY + lev * wnd
                    sims.append({"total_ret_%": 100 * (np.prod(1 + d) - 1), "daily_std_%": 100 * d.std(ddof=1)})
            t = pd.DataFrame(sims)
            row = {"drift_x": ds, "RP_lev": lev,
                   "ret_med_%": t["total_ret_%"].median(), "ret_p10_%": t["total_ret_%"].quantile(0.1),
                   "ret_p90_%": t["total_ret_%"].quantile(0.9), "P(loss)_%": 100 * (t["total_ret_%"] < 0).mean(),
                   "std_med_%": t["daily_std_%"].median()}
            for name, (rr, sr) in FIELDS.items():
                row[f"score_{name}"] = score(t, rr, sr).mean()
            rows.append(row)
    print(f"19-day competitions: {len(windows)} RP windows x {DRAWS} drift resamples")
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
