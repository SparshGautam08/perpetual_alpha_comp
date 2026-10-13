"""Step A maker micro-test (E32): rest small post-only quotes on par around
a fair price from Hyperliquid mainnet, and measure fills and "markouts"
(does the oracle move against us after a fill?).

    python maker_test.py --dry-run                  # no orders: prints quotes for 1 min
    python maker_test.py --minutes 60 --confirm     # live, needs HL_API_KEY_FILE

Fair price per market: par oracle x (1 + premium), where premium = mainnet
mid vs par oracle, minus its rolling median during the session (removes
the usual offset; E20/E23 baseline). Quotes: bid = fair - OFFSET_BP,
ask = fair + OFFSET_BP, never crossing the book (post-only "Alo": the
exchange rejects anything that would trade immediately, so we only pay the
3 bp maker fee). Quotes are replaced when the target moves > REQUOTE_BP.

Safety
- $SIZE_USD per order; a side stops quoting once this script's OWN
  inventory (from its own fills only, so drift trades are never mistaken
  for ours) reaches +-CAP_USD.
- Exchange dead man's switch: every loop, schedule_cancel(now + 60 s), so
  if the script or laptop dies the exchange cancels our quotes itself.
- On exit (time up, Ctrl+C, error): cancel all quotes, then close this
  script's own inventory with small IoC orders.
- Don't run it across 7 PM CT (the main bot's daily rebalance).
Output: maker_test_log_<UTC time>.csv (quotes, fills, markouts) and a
summary at the end.
"""
import argparse
import csv
import os
import signal
import time
from collections import deque
from datetime import datetime, timezone
from statistics import median

import requests

PAR = "https://api.hyperliquid-testnet.xyz"
MAINNET = "https://api.hyperliquid.xyz"
TEAM = os.environ.get("HL_TEAM_ADDRESS", "0xC9f1C5a784d2cd5858396299893e71cfe23F4aAf")
DEX = "par"
MAINNET_COIN = {"BTC": "BTC", "ETH": "ETH", "HYPE": "HYPE"}   # crypto only for step A
LOOP_S = 3
REQUOTE_BP = 2.0
BASELINE_N = 200            # samples in the rolling premium median (~10 min)
MARKOUT_S = (10, 60, 300)

ap = argparse.ArgumentParser()
ap.add_argument("--coins", default="HYPE,BTC,ETH")
ap.add_argument("--minutes", type=float, default=60)
ap.add_argument("--size", type=float, default=500, help="$ per quote")
ap.add_argument("--cap", type=float, default=2000, help="max own inventory $ per market")
ap.add_argument("--offset", type=float, default=6.0, help="bp from fair price")
ap.add_argument("--confirm", action="store_true")
ap.add_argument("--dry-run", action="store_true")
a = ap.parse_args()
COINS = a.coins.split(",")
assert all(c in MAINNET_COIN for c in COINS), "step A quotes crypto only"
if not a.dry_run and not a.confirm:
    raise SystemExit("Live run needs --confirm (or use --dry-run).")
if a.size > 1000 or a.cap > 5000:
    raise SystemExit("Refusing: step A is capped at $1,000 per quote and $5,000 inventory.")

s = requests.Session()
info = lambda url, p: s.post(url + "/info", json=p, timeout=10).json()
running = True
signal.signal(signal.SIGINT, lambda *_: globals().__setitem__("running", False))
signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("running", False))
stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
logf = open(f"maker_test_log_{stamp}.csv", "w", newline="")
log = csv.writer(logf)
log.writerow(["t_ms", "event", "coin", "side", "px", "sz", "oracle", "fair", "detail"])


def now_ms():
    return int(time.time() * 1000)


def say(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] {msg}", flush=True)


def round_px(px, dec):
    return round(float(f"{px:.5g}"), 6 - dec)


def snapshot():
    meta, ctxs = info(PAR, {"type": "metaAndAssetCtxs", "dex": DEX})
    by = {m["name"]: (m, c) for m, c in zip(meta["universe"], ctxs)}
    mids = info(MAINNET, {"type": "allMids"})
    out = {}
    for c in COINS:
        m, ctx = by[f"{DEX}:{c}"]
        book = info(PAR, {"type": "l2Book", "coin": f"{DEX}:{c}"})["levels"]
        out[c] = {"dec": m["szDecimals"], "oracle": float(ctx["oraclePx"]),
                  "bid": float(book[0][0]["px"]) if book[0] else None,
                  "ask": float(book[1][0]["px"]) if book[1] else None,
                  "mn": float(mids[MAINNET_COIN[c]])}
    return out


ex = None
if not a.dry_run:
    import eth_account
    from hyperliquid.exchange import Exchange
    with open(os.path.expanduser(os.environ["HL_API_KEY_FILE"])) as f:
        ex = Exchange(eth_account.Account.from_key(f.read().strip()), PAR,
                      account_address=TEAM, perp_dexs=[DEX])

prem_hist = {c: deque(maxlen=BASELINE_N) for c in COINS}
quotes = {c: {"buy": None, "sell": None} for c in COINS}   # side -> {"oid", "px"}
my_oids = set()
inv = {c: 0.0 for c in COINS}                              # own inventory, units
seen_tids = set()
fills, pending_markouts = [], []
t_start, t_end = time.time(), time.time() + (1 if a.dry_run else a.minutes) * 60


def place(c, side, px, sz):
    r = ex.order(f"{DEX}:{c}", side == "buy", float(sz), float(px), {"limit": {"tif": "Alo"}})
    st = r["response"]["data"]["statuses"][0] if r.get("status") == "ok" else {"error": str(r)}
    if "resting" in st:
        oid = st["resting"]["oid"]
        my_oids.add(oid)
        return oid
    log.writerow([now_ms(), "reject", c, side, px, sz, "", "", st.get("error", st)])
    return None


def cancel(c, side):
    q = quotes[c][side]
    if q and ex:
        try:
            ex.cancel(f"{DEX}:{c}", q["oid"])
        except Exception as e:
            log.writerow([now_ms(), "cancel_err", c, side, "", "", "", "", str(e)])
    quotes[c][side] = None


def poll_fills(snap):
    if not ex:
        return
    for f in info(PAR, {"type": "userFills", "user": TEAM}):
        if f["oid"] not in my_oids or f["tid"] in seen_tids:
            continue
        seen_tids.add(f["tid"])
        c = f["coin"].split(":")[1]
        side = 1 if f["side"] == "B" else -1
        sz, px = float(f["sz"]), float(f["px"])
        inv[c] += side * sz
        o = snap[c]["oracle"]
        rec = {"t": f["time"], "coin": c, "side": side, "px": px, "sz": sz, "fee": float(f["fee"]),
               "oracle": o, "edge_bp": side * (o - px) / px * 1e4, "crossed": f.get("crossed")}
        fills.append(rec)
        for h in MARKOUT_S:
            pending_markouts.append((f["time"] + h * 1000, len(fills) - 1, h))
        log.writerow([f["time"], "fill", c, "buy" if side > 0 else "sell", px, sz, o, "",
                      f"fee={f['fee']} crossed={f.get('crossed')}"])
        say(f"FILL {c} {'BUY' if side > 0 else 'SELL'} {sz} @ {px}  oracle {o}  edge {rec['edge_bp']:+.1f} bp")
        for q in quotes[c].values():                     # a filled quote is gone
            if q and q["oid"] == f["oid"]:
                q["filled"] = True


def do_markouts(snap):
    t = now_ms()
    for item in [m for m in pending_markouts if m[0] <= t]:
        _, i, h = item
        fr = fills[i]
        fr[f"mo_{h}s_bp"] = fr["side"] * (snap[fr["coin"]]["oracle"] - fr["px"]) / fr["px"] * 1e4
        pending_markouts.remove(item)


def loop_once():
    snap = snapshot()
    if ex:
        ex.schedule_cancel(now_ms() + 60_000)            # dead man's switch
    poll_fills(snap)
    do_markouts(snap)
    for c, d in snap.items():
        prem = d["mn"] / d["oracle"] - 1
        prem_hist[c].append(prem)
        if len(prem_hist[c]) < 20:                        # ~1 min warm-up
            continue
        fair = d["oracle"] * (1 + prem - median(prem_hist[c]))
        tgt = {"buy": fair * (1 - a.offset / 1e4), "sell": fair * (1 + a.offset / 1e4)}
        if d["ask"]:
            tgt["buy"] = min(tgt["buy"], d["ask"] * (1 - 1e-4))   # never cross
        if d["bid"]:
            tgt["sell"] = max(tgt["sell"], d["bid"] * (1 + 1e-4))
        for side in ("buy", "sell"):
            px = round_px(tgt[side], d["dec"])
            sz = round(a.size / px, d["dec"])
            sign = 1 if side == "buy" else -1
            over_cap = sign * inv[c] * d["oracle"] >= a.cap
            q = quotes[c][side]
            if q and q.get("filled"):
                quotes[c][side] = q = None
            if over_cap:
                if q:
                    cancel(c, side)
                continue
            if q and abs(q["px"] / px - 1) * 1e4 <= REQUOTE_BP:
                continue
            if a.dry_run:
                say(f"[dry] {c} {side} {sz} @ {px}  (fair {fair:.6g}, oracle {d['oracle']}, "
                    f"book {d['bid']}/{d['ask']})")
                quotes[c][side] = {"oid": None, "px": px}
                continue
            if q:
                cancel(c, side)
            oid = place(c, side, px, sz)
            if oid:
                quotes[c][side] = {"oid": oid, "px": px}
                log.writerow([now_ms(), "quote", c, side, px, sz, d["oracle"], fair, oid])


def shutdown():
    say("Shutting down: cancel quotes, close own inventory")
    for c in COINS:
        for side in ("buy", "sell"):
            cancel(c, side)
    if not ex:
        return
    ex.schedule_cancel(None)
    snap = snapshot()
    for c, units in inv.items():
        dec = snap[c]["dec"]
        units = round(units, dec)
        if abs(units) * snap[c]["oracle"] < 11:
            continue
        is_buy = units < 0
        ref = snap[c]["ask"] if is_buy else snap[c]["bid"]
        px = round_px(ref * (1.002 if is_buy else 0.998), dec)
        r = ex.order(f"{DEX}:{c}", is_buy, abs(units), px, {"limit": {"tif": "Ioc"}})
        say(f"close own {c} {'BUY' if is_buy else 'SELL'} {abs(units)}: {r.get('response', r)}")
        log.writerow([now_ms(), "close", c, "buy" if is_buy else "sell", px, abs(units), "", "", str(r)])


def summary():
    if not fills:
        say("No fills.")
        return
    n = len(fills)
    vol = sum(f["px"] * f["sz"] for f in fills)
    fees = sum(f["fee"] for f in fills)
    avg = lambda k: sum(f[k] for f in fills if k in f) / max(1, sum(k in f for f in fills))
    say(f"SUMMARY fills {n}, volume ${vol:,.0f}, fees ${fees:,.2f} ({fees / vol * 1e4:.1f} bp), "
        f"edge at fill {avg('edge_bp'):+.1f} bp, markout 10 s {avg('mo_10s_bp'):+.1f} / "
        f"60 s {avg('mo_60s_bp'):+.1f} / 300 s {avg('mo_300s_bp'):+.1f} bp, "
        f"crossed {sum(bool(f['crossed']) for f in fills)}")


say(f"{'DRY RUN' if a.dry_run else 'LIVE'}: {COINS}, ${a.size}/quote, cap ${a.cap}, "
    f"offset {a.offset} bp, {a.minutes if not a.dry_run else 1} min")
try:
    while running and time.time() < t_end:
        t0 = time.time()
        try:
            loop_once()
        except Exception as e:
            say(f"loop error: {type(e).__name__}: {e}")
            log.writerow([now_ms(), "error", "", "", "", "", "", "", str(e)])
        logf.flush()
        time.sleep(max(0, LOOP_S - (time.time() - t0)))
finally:
    try:
        shutdown()
    finally:
        summary()
        logf.close()
