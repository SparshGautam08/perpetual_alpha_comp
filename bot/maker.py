"""ML maker (E34): rest post-only quotes on par around the ML fair price.

    python -m bot.maker                                 # paper (no key), runs forever
    python -m bot.maker --minutes 60 --fair base        # compare: simple fair price
    BOT_LIVE=yes HL_MAKER_KEY_FILE=~/keys/api_wallet_bot2.key \\
        python -m bot.maker --mode live --minutes 60 --flatten-on-exit

Fair price per market = where ML Model 1 expects par's oracle to be in 15 s
(oracle x (1 + GBM prediction)); `--fair base` uses the simple premium rule,
`--fair oracle` the current oracle. Quotes: bid/ask = centre -+ OFFSET_BP,
where the centre leans against our own inventory (up to LEAN_BP at the cap)
so quotes that reduce inventory are more likely to fill. Quotes never cross
par's best bid/ask (post-only "Alo": 3 bp maker fee, never 9 bp taker).

Safety: $SIZE per quote; a side stops quoting at +-CAP of own inventory;
live mode refreshes the exchange dead man's switch (orders auto-cancelled
60 s after the process stops); own inventory is tracked from OWN order
fills only and written to state/maker_units_live.json so the combined bot
never mistakes it for risk parity's. Live needs its OWN API key (bot2):
one key per running process.
Paper mode: a resting quote is filled when par's trade feed shows a trade
at ("touch", optimistic) or through ("through", conservative) its price
on the side that would hit it.
Every fill is logged with the oracle at the fill's own second and markouts
at 10 s / 60 s / 300 s (oracle-based, in bp, positive = good for us).
"""
import argparse
import csv
import json
import os
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .livefeed import DEX, LiveFeed, log

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get("BOT_STATE_DIR", str(ROOT / "state")))
PAR = "https://api.hyperliquid-testnet.xyz"
TEAM = os.environ.get("HL_TEAM_ADDRESS", "0xC9f1C5a784d2cd5858396299893e71cfe23F4aAf")
MAKER_FEE_BP = 3.0
MARKOUTS = (10, 60, 300)


def round_px(px, dec):
    return round(float(f"{px:.5g}"), 6 - dec)


class PaperMaker:
    mode = "paper"

    def __init__(self):
        self.resting = {}        # oid -> {coin, side, px, sz}
        self.next_oid = 1
        self.fills = []          # pending fills for the strategy to collect
        self.lock = threading.Lock()

    def place(self, coin, side, px, sz):
        with self.lock:
            oid = self.next_oid
            self.next_oid += 1
            self.resting[oid] = {"coin": coin, "side": side, "px": px, "sz": sz}
            return oid

    def cancel(self, coin, oid):
        with self.lock:
            self.resting.pop(oid, None)

    def heartbeat(self):
        pass

    def on_trade(self, coin, tr):
        """A real par trade. 'A' = a seller hit the bid; 'B' = a buyer lifted the ask."""
        px, sz, agg = float(tr["px"]), float(tr["sz"]), tr["side"]
        with self.lock:
            for oid, q in list(self.resting.items()):
                if q["coin"] != coin:
                    continue
                hit = (q["side"] == "buy" and agg == "A" and px <= q["px"]) or \
                      (q["side"] == "sell" and agg == "B" and px >= q["px"])
                if not hit:
                    continue
                through = px < q["px"] if q["side"] == "buy" else px > q["px"]
                take = min(sz, q["sz"])
                self.fills.append({"oid": oid, "coin": coin, "side": q["side"], "px": q["px"], "sz": take,
                                   "t_ms": tr["time"], "fee": take * q["px"] * MAKER_FEE_BP / 1e4,
                                   "through": through})
                q["sz"] = round(q["sz"] - take, 10)
                if q["sz"] <= 0:
                    self.resting.pop(oid)

    def new_fills(self):
        with self.lock:
            out, self.fills = self.fills, []
        return out


class LiveMaker:
    mode = "live"

    def __init__(self):
        import eth_account
        from hyperliquid.exchange import Exchange
        key = os.path.expanduser(os.environ["HL_MAKER_KEY_FILE"])
        with open(key) as f:
            self.ex = Exchange(eth_account.Account.from_key(f.read().strip()), PAR,
                               account_address=TEAM, perp_dexs=[DEX])
        self.my_oids, self.seen = set(), set()
        self.since = int(time.time() * 1000)

    def place(self, coin, side, px, sz):
        r = self.ex.order(f"{DEX}:{coin}", side == "buy", float(sz), float(px), {"limit": {"tif": "Alo"}})
        st = r["response"]["data"]["statuses"][0] if r.get("status") == "ok" else {"error": str(r)}
        if "resting" in st:
            oid = st["resting"]["oid"]
            self.my_oids.add(oid)
            return oid
        log(f"{coin} {side} @ {px} not placed: {st.get('error', st)}")
        return None

    def cancel(self, coin, oid):
        try:
            self.ex.cancel(f"{DEX}:{coin}", oid)
        except Exception as e:
            log(f"cancel {coin} {oid}: {e}")

    def heartbeat(self):
        self.ex.schedule_cancel(int(time.time() * 1000) + 60_000)    # dead man's switch

    def new_fills(self):
        out = []
        r = requests.post(PAR + "/info", json={"type": "userFillsByTime", "user": TEAM,
                                               "startTime": self.since}, timeout=10).json()
        for f in r:
            if f["oid"] not in self.my_oids or f["tid"] in self.seen:
                continue
            self.seen.add(f["tid"])
            out.append({"oid": f["oid"], "coin": f["coin"].split(":", 1)[1],
                        "side": "buy" if f["side"] == "B" else "sell", "px": float(f["px"]),
                        "sz": float(f["sz"]), "t_ms": f["time"], "fee": float(f["fee"]),
                        "through": None, "crossed": f.get("crossed")})
        return out


class Maker:
    def __init__(self, a, feed, broker):
        self.a, self.feed, self.b = a, feed, broker
        self.quotes = {c: {"buy": None, "sell": None} for c in a.coins}
        meta = requests.post(PAR + "/info", json={"type": "meta", "dex": DEX}, timeout=10).json()
        self.dec = {m["name"].split(":", 1)[1]: m["szDecimals"] for m in meta["universe"]}
        STATE.mkdir(parents=True, exist_ok=True)
        # Paper runs keep separate inventories per fair-price variant (A/B runs
        # side by side); live keeps one, since it trades the real account.
        self.state_path = STATE / (f"maker_paper_{a.fair}.json" if broker.mode == "paper"
                                   else "maker_live.json")
        st = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.inv = {c: float(st.get("inv", {}).get(c, 0.0)) for c in a.coins}
        self.fills, self.pending = [], []
        day = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.logf = open(STATE / f"maker_{broker.mode}_{a.fair}_{day}.csv", "w", newline="")
        self.log = csv.writer(self.logf)
        self.log.writerow(["t_ms", "event", "coin", "side", "px", "sz", "oracle", "fair", "detail"])

    def save(self):
        self.state_path.write_text(json.dumps({"inv": self.inv}, indent=2))
        if self.b.mode == "live":       # tell the combined bot what the maker owns
            (STATE / "maker_units_live.json").write_text(json.dumps(
                {"t": time.time(), "units": {c: u for c, u in self.inv.items() if u}}))

    def _cancel(self, c, side):
        q = self.quotes[c][side]
        if q:
            self.b.cancel(c, q["oid"])
            self.quotes[c][side] = None

    def step(self):
        self.b.heartbeat()
        for f in self.b.new_fills():
            self._record_fill(f)
        self._markouts()
        for c in self.a.coins:
            if not self.feed.ready(c):
                for side in ("buy", "sell"):
                    self._cancel(c, side)
                continue
            x = self.feed.latest[c]
            fair = self.feed.fair(c, self.a.fair)
            frac = max(-1.0, min(1.0, self.inv[c] * x["oracle"] / self.a.cap))
            centre = fair * (1 - self.a.lean * frac / 1e4)
            tgt = {"buy": centre * (1 - self.a.offset / 1e4), "sell": centre * (1 + self.a.offset / 1e4)}
            bbo = self.feed.bbo[c]
            if bbo["ask"]:
                tgt["buy"] = min(tgt["buy"], bbo["ask"] * (1 - 1e-4))
            if bbo["bid"]:
                tgt["sell"] = max(tgt["sell"], bbo["bid"] * (1 + 1e-4))
            for side, sign in (("buy", 1), ("sell", -1)):
                q = self.quotes[c][side]
                if q and self.b.mode == "paper" and q["oid"] not in self.b.resting:
                    self.quotes[c][side] = q = None        # paper quote fully filled
                if sign * self.inv[c] * x["oracle"] >= self.a.cap:     # at the cap: stop adding
                    self._cancel(c, side)
                    continue
                px = round_px(tgt[side], self.dec[c])
                if q and abs(q["px"] / px - 1) * 1e4 <= self.a.requote:
                    continue
                self._cancel(c, side)
                sz = round(self.a.size / px, self.dec[c])
                oid = self.b.place(c, side, px, sz)
                if oid:
                    self.quotes[c][side] = {"oid": oid, "px": px}
                    self.log.writerow([int(time.time() * 1000), "quote", c, side, px, sz, x["oracle"],
                                       round(fair, 6), oid])
        self.logf.flush()

    def _record_fill(self, f):
        c, sign = f["coin"], 1 if f["side"] == "buy" else -1
        self.inv[c] = round(self.inv[c] + sign * f["sz"], 10)
        o = self.feed.oracle_at(c, f["t_ms"] // 1000)
        rec = dict(f, oracle=o, edge_bp=(sign * (o - f["px"]) / f["px"] * 1e4) if o else None)
        self.fills.append(rec)
        for h in MARKOUTS:
            self.pending.append((f["t_ms"] // 1000 + h, len(self.fills) - 1, h))
        for side in ("buy", "sell"):
            q = self.quotes[c][side]
            if q and q["oid"] == f["oid"] and self.b.mode == "live":
                self.quotes[c][side] = None          # live: a filled post-only order is gone
        self.log.writerow([f["t_ms"], "fill", c, f["side"], f["px"], f["sz"], o, "",
                           f"edge_bp={rec['edge_bp']} fee={f['fee']:.4f} through={f.get('through')}"])
        log(f"FILL {c} {f['side'].upper()} {f['sz']} @ {f['px']}  oracle {o}  "
            f"edge {rec['edge_bp'] if rec['edge_bp'] is None else round(rec['edge_bp'], 1)} bp  inv {self.inv[c]}")
        self.save()

    def _markouts(self):
        now = int(time.time())
        for item in [p for p in self.pending if p[0] <= now]:
            t, i, h = item
            fr = self.fills[i]
            o = self.feed.oracle_at(fr["coin"], t)
            if o:
                sign = 1 if fr["side"] == "buy" else -1
                fr[f"mo_{h}s"] = sign * (o - fr["px"]) / fr["px"] * 1e4
                self.log.writerow([t * 1000, f"markout_{h}s", fr["coin"], fr["side"], fr["px"], fr["sz"],
                                   o, "", round(fr[f"mo_{h}s"], 2)])
            self.pending.remove(item)

    def summary(self):
        if not self.fills:
            log("SUMMARY: no fills")
            return
        avg = lambda k, rows: (sum(r[k] for r in rows if r.get(k) is not None) /
                               max(1, sum(r.get(k) is not None for r in rows)))
        vol = sum(r["px"] * r["sz"] for r in self.fills)
        through = [r for r in self.fills if r.get("through")]
        log(f"SUMMARY ({self.b.mode}, fair={self.a.fair}): fills {len(self.fills)} "
            f"(buys {sum(r['side'] == 'buy' for r in self.fills)}), volume ${vol:,.0f}, "
            f"edge at fill {avg('edge_bp', self.fills):+.1f} bp, markout 10 s {avg('mo_10s', self.fills):+.1f} / "
            f"60 s {avg('mo_60s', self.fills):+.1f} / 300 s {avg('mo_300s', self.fills):+.1f} bp, "
            f"fees ${sum(r['fee'] for r in self.fills):,.2f}; inventory {self.inv}"
            + (f"; 'through' fills only: {len(through)}, markout 60 s {avg('mo_60s', through):+.1f} bp"
               if self.b.mode == "paper" else ""))

    def shutdown(self, flatten):
        for c in self.a.coins:
            for side in ("buy", "sell"):
                self._cancel(c, side)
        if self.b.mode == "live":
            self.b.ex.schedule_cancel(None)
            if flatten:
                for c, units in self.inv.items():
                    x = self.feed.latest.get(c)
                    if not x or abs(units) * x["oracle"] < 11:
                        continue
                    is_buy = units < 0
                    px = round_px(x["oracle"] * (1.002 if is_buy else 0.998), self.dec[c])
                    r = self.b.ex.order(f"{DEX}:{c}", is_buy, round(abs(units), self.dec[c]), px,
                                        {"limit": {"tif": "Ioc"}})
                    log(f"flatten {c}: {r.get('response', r)}")
                    st = r.get("response", {}).get("data", {}).get("statuses", [{}])[0]
                    if "filled" in st:
                        self.inv[c] = round(units + (1 if is_buy else -1) * float(st["filled"]["totalSz"]), 10)
        self.save()
        self.logf.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["paper", "live"], default="paper")
    ap.add_argument("--coins", default="BTC,ETH,HYPE")
    ap.add_argument("--fair", choices=["gbm", "base", "oracle"], default="gbm")
    ap.add_argument("--size", type=float, default=500)
    ap.add_argument("--cap", type=float, default=2000)
    ap.add_argument("--offset", type=float, default=6.0, help="bp from the centre")
    ap.add_argument("--lean", type=float, default=6.0, help="bp shift of the centre at full inventory")
    ap.add_argument("--requote", type=float, default=2.0)
    ap.add_argument("--loop", type=float, default=2.0)
    ap.add_argument("--minutes", type=float, default=0, help="0 = run forever")
    ap.add_argument("--flatten-on-exit", action="store_true")
    a = ap.parse_args()
    a.coins = a.coins.split(",")
    if a.size > 1000 or a.cap > 5000:
        raise SystemExit("Refusing: capped at $1,000 per quote and $5,000 inventory per market.")
    if a.mode == "live" and (os.environ.get("BOT_LIVE") != "yes" or not os.environ.get("HL_MAKER_KEY_FILE")):
        raise SystemExit("Live maker needs BOT_LIVE=yes and HL_MAKER_KEY_FILE (its own key, e.g. bot2).")

    broker = PaperMaker() if a.mode == "paper" else LiveMaker()
    feed = LiveFeed(a.coins, with_book=True)
    if a.mode == "paper":
        feed.on_trade = broker.on_trade
    m = Maker(a, feed, broker)
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    log(f"Maker {a.mode}: {a.coins}, fair={a.fair}, ${a.size}/quote, cap ${a.cap}, offset {a.offset} bp, "
        f"lean {a.lean} bp; warming up (~5 min)")
    end = time.time() + a.minutes * 60 if a.minutes else None
    try:
        while not stop["now"] and (end is None or time.time() < end):
            t0 = time.time()
            try:
                m.step()
            except Exception as e:
                log(f"step error {type(e).__name__}: {e}")
            time.sleep(max(0, a.loop - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        m.shutdown(a.flatten_on_exit)
        m.summary()
        feed.stop()


if __name__ == "__main__":
    main()
