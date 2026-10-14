"""Live market feed + ML Model 1, shared by shadow mode and the maker.

LiveFeed keeps websockets to par (oracle, book mid, best bid/ask, trades)
and mainnet (best bid/ask mid, oracle), builds the same 1-second grid the
recorder produced, and every second computes the model inputs with
bot/features.compute_features and the GBM / baseline predictions of par's
oracle move over the next 15 s.

    feed = LiveFeed(coins)            # starts its threads
    feed.latest["BTC"]                # dict: t, oracle, pred_gbm_bp, pred_base_bp, ...
    feed.fair("BTC")                  # oracle x (1 + GBM prediction): where the
                                      #   oracle is expected to be in 15 s
    feed.bbo["BTC"]                   # par best bid/ask (live)
    feed.on_trade = callback          # called with each par trade (paper fills)
"""
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import websocket

from .features import FEATURES, PAIRS, WARMUP_S, compute_features

ROOT = Path(__file__).resolve().parent.parent
PAR_WS = "wss://api.hyperliquid-testnet.xyz/ws"
MN_WS = "wss://api.hyperliquid.xyz/ws"
DEX = os.environ.get("HL_DEX", "par")
STALE_S, PING_S = 30, 45
HIST = WARMUP_S + 120
MIN_ROWS = 310                      # rows before inputs are complete (5 min windows)


def log(msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


class LiveFeed:
    def __init__(self, coins=None, with_book=False, model="oracle_gbm_v1"):
        self.coins = list(coins or PAIRS)
        self.with_book = with_book
        self.gbm = joblib.load(ROOT / "models" / f"{model}.joblib")
        self.base = joblib.load(ROOT / "models" / "oracle_baseline_v1.joblib")
        self.lock = threading.Lock()
        self.raw = {c: {"o": None, "pmid": None, "m": None, "mo": None} for c in self.coins}
        self.bbo = {c: {"bid": None, "ask": None, "t": 0} for c in self.coins}
        self.hist = {c: deque(maxlen=HIST) for c in self.coins}
        self.oracle_hist = {c: deque(maxlen=3600) for c in self.coins}   # (t, oracle), for markouts
        self.latest = {}
        self.on_trade = None
        self.on_second = None            # callback(t, latest) after each prediction round
        self.running = True
        self._mn_to_par = {PAIRS[c]: c for c in self.coins}
        par_subs = [{"type": "activeAssetCtx", "coin": f"{DEX}:{c}"} for c in self.coins]
        if with_book:
            par_subs += [{"type": t, "coin": f"{DEX}:{c}"} for c in self.coins for t in ("bbo", "trades")]
        mn_subs = ([{"type": "bbo", "coin": PAIRS[c]} for c in self.coins]
                   + [{"type": "activeAssetCtx", "coin": PAIRS[c]} for c in self.coins])
        for url, subs, h, name in ((PAR_WS, par_subs, self._on_par, "par"),
                                   (MN_WS, mn_subs, self._on_mainnet, "mainnet")):
            threading.Thread(target=self._feed, args=(url, subs, h, name), daemon=True).start()
        threading.Thread(target=self._clock, daemon=True).start()

    # ---- websockets ------------------------------------------------------
    def _feed(self, url, subs, handle, name):
        backoff = 1
        while self.running:
            last = [time.time()]

            def on_message(_ws, m):
                last[0] = time.time()
                if m.startswith("{"):
                    d = json.loads(m)
                    try:
                        handle(d.get("channel"), d.get("data"))
                    except Exception as e:      # never let one bad message kill the feed
                        log(f"{name}: message error {type(e).__name__}: {e}")

            def on_open(ws):
                for s in subs:
                    ws.send(json.dumps({"method": "subscribe", "subscription": s}))
                log(f"{name}: connected, {len(subs)} feeds")

            ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message)
            t = threading.Thread(target=ws.run_forever, daemon=True)
            t.start()
            started, last_ping = time.time(), time.time()
            while self.running and t.is_alive() and time.time() - last[0] < STALE_S:
                time.sleep(1)
                if time.time() - last_ping > PING_S:
                    try:
                        ws.send('{"method":"ping"}')
                    except Exception:
                        break
                    last_ping = time.time()
            ws.close()
            backoff = 1 if time.time() - started > 60 else min(backoff * 2, 60)
            if self.running:
                time.sleep(backoff)

    def _on_par(self, ch, d):
        if ch == "activeAssetCtx":
            c = d["coin"].split(":", 1)[1]
            if c in self.raw:
                with self.lock:
                    self.raw[c]["o"] = float(d["ctx"]["oraclePx"])
                    if d["ctx"].get("midPx") is not None:
                        self.raw[c]["pmid"] = float(d["ctx"]["midPx"])
        elif ch == "bbo":
            c = d["coin"].split(":", 1)[1]
            if c in self.bbo:
                bid, ask = d.get("bbo") or [None, None]
                with self.lock:
                    self.bbo[c] = {"bid": float(bid["px"]) if bid else None,
                                   "ask": float(ask["px"]) if ask else None, "t": time.time()}
        elif ch == "trades" and self.on_trade:
            for tr in d:
                c = tr["coin"].split(":", 1)[1]
                if c in self.raw:
                    self.on_trade(c, tr)

    def _on_mainnet(self, ch, d):
        c = self._mn_to_par.get(d.get("coin")) if isinstance(d, dict) else None
        if c is None:
            return
        with self.lock:
            if ch == "bbo":
                bid, ask = d.get("bbo") or [None, None]
                if bid and ask:
                    self.raw[c]["m"] = (float(bid["px"]) + float(ask["px"])) / 2
            elif ch == "activeAssetCtx":
                self.raw[c]["mo"] = float(d["ctx"]["oraclePx"])

    # ---- 1-second grid and predictions ------------------------------------
    def _clock(self):
        next_t = int(time.time()) + 1
        while self.running:
            time.sleep(max(0, next_t - time.time()))
            with self.lock:
                snap = {c: dict(v) for c, v in self.raw.items()}
            behind = int(time.time()) - next_t
            if behind > 2:          # fell behind: forward-fill, like the training grid
                log(f"WARNING: {behind} s behind; forward-filling missed seconds")
                for k in range(behind):
                    self._append(pd.Timestamp(next_t + k, unit="s", tz="UTC"), snap)
                next_t += behind
            ts = pd.Timestamp(next_t, unit="s", tz="UTC")
            next_t += 1
            self._append(ts, snap)
            try:
                self._predict(ts, snap)
            except Exception as e:
                log(f"predict error {type(e).__name__}: {e}")
            if self.on_second:
                try:
                    self.on_second(ts, self.latest)
                except Exception as e:
                    log(f"on_second error {type(e).__name__}: {e}")

    def _append(self, ts, snap):
        for c in self.coins:
            v = snap[c]
            if None not in v.values():
                self.hist[c].append((ts, v["o"], v["pmid"], v["m"], v["mo"]))
                self.oracle_hist[c].append((ts.value // 10**9, v["o"]))

    def _predict(self, ts, snap):
        rows, meta = [], []
        for c in self.coins:
            if len(self.hist[c]) < MIN_ROWS:
                continue
            g = pd.DataFrame(list(self.hist[c]), columns=["t", "o", "pmid", "m", "mo"]).set_index("t")
            f = compute_features(g, c).iloc[-1]
            if f[FEATURES].isna().any():
                continue
            rows.append(f[FEATURES].to_numpy(dtype=float))
            meta.append((c, snap[c]["o"], f["prem_dm"], f["o_stale_s"]))
        if not rows:
            return
        X = pd.DataFrame(np.vstack(rows), columns=FEATURES)
        pg, pb = self.gbm.predict(X), self.base.predict(X[["prem_dm"]])
        t = ts.value // 10**9
        for (c, o, pdm, st), g_, b_ in zip(meta, pg, pb):
            self.latest[c] = {"t": t, "oracle": o, "pred_gbm_bp": float(g_), "pred_base_bp": float(b_),
                              "prem_dm": float(pdm), "o_stale_s": int(st)}

    # ---- helpers -----------------------------------------------------------
    def ready(self, c, max_age_s=5):
        x = self.latest.get(c)
        return x is not None and time.time() - x["t"] <= max_age_s

    def fair(self, c, use="gbm"):
        """Where the oracle is expected to be in 15 s (model), or now (use='oracle')."""
        x = self.latest[c]
        if use == "oracle":
            return x["oracle"]
        p = x["pred_gbm_bp"] if use == "gbm" else x["pred_base_bp"]
        return x["oracle"] * (1 + p / 1e4)

    def oracle_at(self, c, t_s):
        """Oracle at or before unix second t_s (None if out of range)."""
        for t, o in reversed(self.oracle_hist[c]):
            if t <= t_s:
                return o
        return None

    def stop(self):
        self.running = False
