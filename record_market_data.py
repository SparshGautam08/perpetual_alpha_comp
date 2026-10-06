"""Record live market data for all competition markets, 24/7.

No private key needed. It only reads public data.

Every INTERVAL seconds it saves, for each of the 10 markets on the `par` exchange:
  - ctx_<date>.csv : oracle price, mark price, mid price, funding, open interest,
                     premium, 24h volume, impact prices
  - book_<date>.csv: top N levels of the order book (bid/ask price, size, order count)

Files go in ./data/ and roll over to a new file each UTC day.
If the network drops or the API errors, it logs the problem, waits, and keeps going.

Usage:
  python record_market_data.py              # every 5 seconds, top 5 book levels
  python record_market_data.py --interval 2 --levels 10
Stop with Ctrl+C.
"""
import argparse
import csv
import os
import signal
import sys
import time
from datetime import datetime, timezone

import requests

BASE_URL = os.environ.get("HL_BASE_URL", "https://api.hyperliquid-testnet.xyz")
DEX = os.environ.get("HL_DEX", "par")
INFO_URL = BASE_URL.rstrip("/") + "/info"

p = argparse.ArgumentParser()
p.add_argument("--interval", type=float, default=5.0, help="seconds between snapshots")
p.add_argument("--levels", type=int, default=5, help="order book levels per side")
p.add_argument("--out", default="data", help="output folder")
args = p.parse_args()

os.makedirs(args.out, exist_ok=True)
session = requests.Session()
running = True


def stop(*_):
    global running
    running = False
    print("\nStopping after this cycle...")


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}Z] {msg}", flush=True)


def post(payload):
    r = session.post(INFO_URL, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


CTX_FIELDS = ["ts_ms", "coin", "oraclePx", "markPx", "midPx", "funding", "openInterest",
              "premium", "dayNtlVlm", "prevDayPx", "impactBid", "impactAsk"]
BOOK_FIELDS = ["ts_ms", "book_ts_ms", "coin"]
for side in ("bid", "ask"):
    for i in range(1, args.levels + 1):
        BOOK_FIELDS += [f"{side}{i}_px", f"{side}{i}_sz", f"{side}{i}_n"]


class DailyCsv:
    """Appends rows to <prefix>_<UTC date>.csv, starting a new file each day."""

    def __init__(self, prefix, fields):
        self.prefix, self.fields = prefix, fields
        self.day, self.f, self.w = None, None, None

    def write(self, rows):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day != self.day:
            if self.f:
                self.f.close()
            path = os.path.join(args.out, f"{self.prefix}_{day}.csv")
            new = not os.path.exists(path)
            self.f = open(path, "a", newline="")
            self.w = csv.DictWriter(self.f, fieldnames=self.fields)
            if new:
                self.w.writeheader()
            self.day = day
        self.w.writerows(rows)
        self.f.flush()

    def close(self):
        if self.f:
            self.f.close()


def snapshot_ctx(ts):
    meta, ctxs = post({"type": "metaAndAssetCtxs", "dex": DEX})
    rows = []
    for asset, ctx in zip(meta["universe"], ctxs):
        impact = ctx.get("impactPxs") or [None, None]
        rows.append({
            "ts_ms": ts, "coin": asset["name"],
            "oraclePx": ctx.get("oraclePx"), "markPx": ctx.get("markPx"),
            "midPx": ctx.get("midPx"), "funding": ctx.get("funding"),
            "openInterest": ctx.get("openInterest"), "premium": ctx.get("premium"),
            "dayNtlVlm": ctx.get("dayNtlVlm"), "prevDayPx": ctx.get("prevDayPx"),
            "impactBid": impact[0], "impactAsk": impact[1],
        })
    return rows


def snapshot_book(ts, coin):
    book = post({"type": "l2Book", "coin": coin})
    row = {"ts_ms": ts, "book_ts_ms": book.get("time"), "coin": coin}
    for side, levels in zip(("bid", "ask"), book.get("levels", [[], []])):
        for i in range(args.levels):
            lvl = levels[i] if i < len(levels) else {}
            row[f"{side}{i+1}_px"] = lvl.get("px")
            row[f"{side}{i+1}_sz"] = lvl.get("sz")
            row[f"{side}{i+1}_n"] = lvl.get("n")
    return row


def main():
    meta = post({"type": "meta", "dex": DEX})
    coins = [a["name"] for a in meta["universe"]]
    log(f"Recording {len(coins)} markets on '{DEX}': {', '.join(coins)}")
    log(f"Every {args.interval}s, top {args.levels} book levels, saving to ./{args.out}/")

    ctx_csv, book_csv = DailyCsv("ctx", CTX_FIELDS), DailyCsv("book", BOOK_FIELDS)
    cycles, errors, backoff = 0, 0, args.interval
    while running:
        start = time.time()
        ts = int(start * 1000)
        try:
            ctx_csv.write(snapshot_ctx(ts))
            book_csv.write([snapshot_book(ts, c) for c in coins])
            cycles += 1
            backoff = args.interval
            if cycles % max(1, int(600 / args.interval)) == 0:  # status line ~every 10 min
                log(f"OK: {cycles} snapshots saved, {errors} errors so far")
        except Exception as e:  # network blips, rate limits, bad responses
            errors += 1
            backoff = min(backoff * 2, 60)
            log(f"Error ({type(e).__name__}: {e}). Retrying in {backoff:.0f}s")
            time.sleep(backoff)
            continue
        time.sleep(max(0, args.interval - (time.time() - start)))

    ctx_csv.close()
    book_csv.close()
    log(f"Stopped. {cycles} snapshots saved, {errors} errors.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"Could not start: {e}")
        sys.exit(1)
