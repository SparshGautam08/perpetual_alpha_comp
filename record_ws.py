"""Record live par market data from Hyperliquid's websocket, 24/7.

No private key needed. It only reads public data. Replaces the 5-second
polling of record_market_data.py with pushed updates:

  ctxws_<date>.csv : oracle, mark, mid, funding, premium, impact prices, OI.
                     Pushed ~every 1 s; a row is written when any of them
                     changes.
  bbo_<date>.csv   : best bid / best ask. Pushed by the exchange the moment
                     either changes, with the exchange timestamp. (mainnet:
                     only rows where a price changed, not just a size.)
  l2_<date>.csv    : full order book, up to LEVELS per side (the par book has
                     ~8). Pushed ~every 5 s; a row is written only when the
                     book changed.

Every row has recv_ms (our clock when the message arrived). bbo and l2 rows
also have ex_ms (the exchange's timestamp). Files roll over each UTC day.

Reliability: reconnects with backoff on any error or close, and forces a
reconnect if no message arrives for STALE_S seconds. Status line every
10 minutes. Stop with Ctrl+C or SIGTERM.

With --venue mainnet it records the real Hyperliquid markets that par's
markets copy (BTC, xyz:SP500, ...), as a lead signal for par's oracle. Only
ctxws and bbo are written (mainnet's full book updates every block and is
too big). Files get an mn_ prefix. Run it as a separate process.

Usage:
  python record_ws.py                                   # par, ./data_ws/
  python record_ws.py --venue mainnet --out data_mainnet
Needs: requests, websocket-client
"""
import argparse
import csv
import json
import os
import signal
import threading
import time
from datetime import datetime, timezone

import requests
import websocket

LEVELS = 20        # l2Book returns at most 20 levels per side
STALE_S = 30       # no message this long -> reconnect
PING_S = 45        # server drops idle connections after 60 s
# par market -> Hyperliquid mainnet market it copies (historical/README.md)
MAINNET_COINS = ["BTC", "ETH", "HYPE", "xyz:SP500", "xyz:EWY", "xyz:EWJ",
                 "xyz:BRENTOIL", "xyz:GOLD", "xyz:EUR", "xyz:JPY"]

p = argparse.ArgumentParser()
p.add_argument("--venue", choices=["par", "mainnet"], default="par")
p.add_argument("--out", default=None, help="output folder (default data_ws / data_mainnet)")
args = p.parse_args()
MAINNET = args.venue == "mainnet"
args.out = args.out or ("data_mainnet" if MAINNET else "data_ws")
os.makedirs(args.out, exist_ok=True)
BASE_URL = ("https://api.hyperliquid.xyz" if MAINNET
            else os.environ.get("HL_BASE_URL", "https://api.hyperliquid-testnet.xyz"))
DEX = os.environ.get("HL_DEX", "par")
WS_URL = "wss" + BASE_URL[len("https"):] + "/ws"
FEEDS = ("activeAssetCtx", "bbo") if MAINNET else ("activeAssetCtx", "bbo", "l2Book")
PREFIX = "mn_" if MAINNET else ""


def log(msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


class DailyCsv:
    """Appends rows to <prefix>_<UTC date>.csv, starting a new file each day.
    Flushes at most once a second to keep disk writes low."""

    def __init__(self, prefix, fields):
        self.prefix, self.fields = prefix, fields
        self.day, self.f, self.w, self.last_flush = None, None, None, 0.0
        self.lock = threading.Lock()

    def write(self, row):
        with self.lock:
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
            self.w.writerow(row)
            if time.time() - self.last_flush > 1:
                self.f.flush()
                self.last_flush = time.time()

    def close(self):
        with self.lock:
            if self.f:
                self.f.close()


CTX_KEYS = ["oraclePx", "markPx", "midPx", "funding", "premium", "openInterest",
            "dayNtlVlm"]
ctx_csv = DailyCsv(PREFIX + "ctxws", ["recv_ms", "coin"] + CTX_KEYS + ["impactBid", "impactAsk"])
bbo_csv = DailyCsv(PREFIX + "bbo", ["recv_ms", "ex_ms", "coin", "bid_px", "bid_sz", "bid_n",
                           "ask_px", "ask_sz", "ask_n"])
l2_fields = ["recv_ms", "ex_ms", "coin"]
for side in ("bid", "ask"):
    for i in range(1, LEVELS + 1):
        l2_fields += [f"{side}{i}_px", f"{side}{i}_sz", f"{side}{i}_n"]
l2_csv = DailyCsv("l2", l2_fields)

last_ctx, last_l2, last_bbo_px = {}, {}, {}   # coin -> last written values
counts = {"ctx": 0, "bbo": 0, "l2": 0}
last_msg = time.time()
running = True


def on_message(_ws, message):
    global last_msg
    last_msg = time.time()
    if not message.startswith("{"):
        return
    msg = json.loads(message)
    ch, d = msg.get("channel"), msg.get("data")
    now = int(time.time() * 1000)
    if ch == "activeAssetCtx":
        c = d["ctx"]
        imp = c.get("impactPxs") or [None, None]
        vals = tuple(c.get(k) for k in ("oraclePx", "markPx", "midPx", "funding")) + tuple(imp)
        if last_ctx.get(d["coin"]) == vals:
            return
        last_ctx[d["coin"]] = vals
        ctx_csv.write({"recv_ms": now, "coin": d["coin"],
                       **{k: c.get(k) for k in CTX_KEYS},
                       "impactBid": imp[0], "impactAsk": imp[1]})
        counts["ctx"] += 1
    elif ch == "bbo":
        bid, ask = d.get("bbo") or [None, None]
        bid, ask = bid or {}, ask or {}     # a side can be empty (None)
        if MAINNET:   # busy book: keep price changes only, skip size-only updates
            px = (bid.get("px"), ask.get("px"))
            if last_bbo_px.get(d["coin"]) == px:
                return
            last_bbo_px[d["coin"]] = px
        bbo_csv.write({"recv_ms": now, "ex_ms": d.get("time"), "coin": d["coin"],
                       "bid_px": bid.get("px"), "bid_sz": bid.get("sz"), "bid_n": bid.get("n"),
                       "ask_px": ask.get("px"), "ask_sz": ask.get("sz"), "ask_n": ask.get("n")})
        counts["bbo"] += 1
    elif ch == "l2Book":
        levels = d.get("levels", [[], []])
        key = json.dumps(levels)
        if last_l2.get(d["coin"]) == key:
            return
        last_l2[d["coin"]] = key
        row = {"recv_ms": now, "ex_ms": d.get("time"), "coin": d["coin"]}
        for side, lv in zip(("bid", "ask"), levels):
            for i, lvl in enumerate(lv[:LEVELS]):
                row[f"{side}{i+1}_px"] = lvl["px"]
                row[f"{side}{i+1}_sz"] = lvl["sz"]
                row[f"{side}{i+1}_n"] = lvl["n"]
        l2_csv.write(row)
        counts["l2"] += 1


def run_once(coins):
    """One websocket session. Returns when the connection closes or goes stale."""
    def on_open(ws):
        for c in coins:
            for t in FEEDS:
                ws.send(json.dumps({"method": "subscribe",
                                    "subscription": {"type": t, "coin": c}}))
        log(f"Connected, subscribed to {len(coins) * len(FEEDS)} feeds")

    ws = websocket.WebSocketApp(WS_URL, on_open=on_open, on_message=on_message,
                                on_error=lambda _w, e: log(f"Websocket error: {e}"))
    t = threading.Thread(target=ws.run_forever, daemon=True)
    t.start()
    global last_msg
    last_msg = last_ping = time.time()
    while running and t.is_alive():
        time.sleep(1)
        if time.time() - last_msg > STALE_S:
            log(f"No data for {STALE_S}s, reconnecting")
            break
        if time.time() - last_ping > PING_S:   # Hyperliquid wants a JSON ping
            try:
                ws.send('{"method":"ping"}')
            except Exception:
                break
            last_ping = time.time()
    ws.close()
    t.join(timeout=5)


def status_loop():
    while running:
        for _ in range(600):
            if not running:
                return
            time.sleep(1)
        log(f"OK: rows written ctx={counts['ctx']} bbo={counts['bbo']} l2={counts['l2']}")


def stop(*_):
    global running
    running = False
    log("Stopping...")


def main():
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    if MAINNET:
        coins = MAINNET_COINS
    else:
        meta = requests.post(BASE_URL + "/info", json={"type": "meta", "dex": DEX},
                             timeout=10).json()
        coins = [a["name"] for a in meta["universe"]]
    log(f"Recording {len(coins)} {args.venue} markets via {WS_URL} to ./{args.out}/")
    threading.Thread(target=status_loop, daemon=True).start()
    backoff = 1
    while running:
        started = time.time()
        try:
            run_once(coins)
        except Exception as e:
            log(f"Session failed ({type(e).__name__}: {e})")
        if not running:
            break
        backoff = 1 if time.time() - started > 60 else min(backoff * 2, 60)
        log(f"Reconnecting in {backoff}s")
        time.sleep(backoff)
    for f in (ctx_csv, bbo_csv, l2_csv):
        f.close()
    log(f"Stopped. Rows written ctx={counts['ctx']} bbo={counts['bbo']} l2={counts['l2']}")


if __name__ == "__main__":
    main()
