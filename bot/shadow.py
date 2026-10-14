"""Shadow mode for ML Model 1: live predictions, NO trading, NO key needed.

    python -m bot.shadow                        # writes shadow/shadow_<UTC date>.csv
    python -m bot.shadow --out shadow --hours 1

Uses bot/livefeed.LiveFeed, the SAME feed and model code the maker uses, so
what shadow mode validates is exactly what trades. Every second it logs,
per market, the par oracle and the GBM and baseline predictions of the
oracle's move over the next 15 s. backtest/eval_shadow.py scores the log.
"""
import argparse
import csv
import time
from pathlib import Path

from .livefeed import ROOT, LiveFeed, log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "shadow"))
    ap.add_argument("--hours", type=float, default=0, help="0 = run forever")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    state = {"day": None, "f": None, "w": None, "n": 0, "last_t": 0}

    def on_second(ts, latest):
        today = ts.strftime("%Y-%m-%d")
        if today != state["day"]:
            if state["f"]:
                state["f"].close()
            path = out / f"shadow_{today}.csv"
            new = not path.exists()
            state["f"] = open(path, "a", newline="")
            state["w"] = csv.writer(state["f"])
            if new:
                state["w"].writerow(["t", "coin", "oracle", "pred_gbm_bp", "pred_base_bp", "prem_dm", "o_stale_s"])
            state["day"] = today
        t = ts.value // 10**9
        for c, x in latest.items():
            if x["t"] == t:
                state["w"].writerow([t, c, x["oracle"], round(x["pred_gbm_bp"], 4),
                                     round(x["pred_base_bp"], 4), round(x["prem_dm"], 4), x["o_stale_s"]])
                state["n"] += 1
        state["f"].flush()
        if t % 600 == 0:
            log(f"OK: {state['n']} predictions logged; markets ready {len(latest)}")

    feed = LiveFeed()
    feed.on_second = on_second
    log(f"Shadow mode: {len(feed.coins)} markets, writing to {out}/")
    end = time.time() + a.hours * 3600 if a.hours else None
    try:
        while end is None or time.time() < end:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        feed.stop()
        if state["f"]:
            state["f"].close()
        log(f"Stopped. {state['n']} predictions logged.")


if __name__ == "__main__":
    main()
