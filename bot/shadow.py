"""Shadow mode for ML Model 1: live predictions, NO trading, NO key needed.

    python -m bot.shadow                        # writes shadow/shadow_<UTC date>.csv
    python -m bot.shadow --out shadow --hours 1

Every second, for each of the 10 markets:
1. take the latest live values (par oracle and book mid from par's
   activeAssetCtx; mainnet best bid/ask mid from mainnet's bbo; mainnet
   oracle from mainnet's activeAssetCtx), exactly as the recorder grids them;
2. compute the inputs with bot/features.compute_features (the same function
   used for training);
3. log the GBM prediction and the baseline prediction of the par oracle's
   move over the next 15 s.
backtest/eval_shadow.py then scores the predictions against what the
oracle actually did.
Websockets reconnect on error, on silence (STALE_S) and on Hyperliquid's
~11-minute expiry; snapshots keep forward-filling meanwhile, as in the
recorded data the model was trained on.
"""
import argparse
import csv
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
HIST = WARMUP_S + 120                      # seconds of history kept per market

latest = {c: {"o": None, "pmid": None, "m": None, "mo": None} for c in PAIRS}
lock = threading.Lock()
running = True


def log(msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def feed(url, subs, handle, name):
    """Keep one websocket alive forever, calling handle(channel, data)."""
    backoff = 1
    while running:
        last = [time.time()]

        def on_message(_ws, m):
            last[0] = time.time()
            if m.startswith("{"):
                d = json.loads(m)
                handle(d.get("channel"), d.get("data"))

        def on_open(ws):
            for s in subs:
                ws.send(json.dumps({"method": "subscribe", "subscription": s}))
            log(f"{name}: connected, {len(subs)} feeds")

        ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message)
        t = threading.Thread(target=ws.run_forever, daemon=True)
        t.start()
        started, last_ping = time.time(), time.time()
        while running and t.is_alive() and time.time() - last[0] < STALE_S:
            time.sleep(1)
            if time.time() - last_ping > PING_S:
                try:
                    ws.send('{"method":"ping"}')
                except Exception:
                    break
                last_ping = time.time()
        ws.close()
        backoff = 1 if time.time() - started > 60 else min(backoff * 2, 60)
        if running:
            time.sleep(backoff)


def on_par(ch, d):
    if ch == "activeAssetCtx":
        c = d["coin"].split(":", 1)[1]
        if c in latest:
            with lock:
                latest[c]["o"] = float(d["ctx"]["oraclePx"])
                mid = d["ctx"].get("midPx")
                if mid is not None:
                    latest[c]["pmid"] = float(mid)


MN_TO_PAR = {mn: c for c, mn in PAIRS.items()}


def on_mainnet(ch, d):
    c = MN_TO_PAR.get(d.get("coin")) if isinstance(d, dict) else None
    if c is None:
        return
    with lock:
        if ch == "bbo":
            bid, ask = d.get("bbo") or [None, None]
            if bid and ask:
                latest[c]["m"] = (float(bid["px"]) + float(ask["px"])) / 2
        elif ch == "activeAssetCtx":
            latest[c]["mo"] = float(d["ctx"]["oraclePx"])


def main():
    global running
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "shadow"))
    ap.add_argument("--hours", type=float, default=0, help="0 = run forever")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    gbm = joblib.load(ROOT / "models" / "oracle_gbm_v1.joblib")
    base = joblib.load(ROOT / "models" / "oracle_baseline_v1.joblib")
    log(f"Shadow mode: {len(PAIRS)} markets, model oracle_gbm_v1, writing to {out}/")

    par_subs = [{"type": "activeAssetCtx", "coin": f"{DEX}:{c}"} for c in PAIRS]
    mn_subs = ([{"type": "bbo", "coin": mn} for mn in PAIRS.values()]
               + [{"type": "activeAssetCtx", "coin": mn} for mn in PAIRS.values()])
    threading.Thread(target=feed, args=(PAR_WS, par_subs, on_par, "par"), daemon=True).start()
    threading.Thread(target=feed, args=(MN_WS, mn_subs, on_mainnet, "mainnet"), daemon=True).start()

    hist = {c: deque(maxlen=HIST) for c in PAIRS}
    day, f, w = None, None, None
    end = time.time() + a.hours * 3600 if a.hours else None
    n_pred = 0
    next_t = int(time.time()) + 1
    try:
        while running and (end is None or time.time() < end):
            time.sleep(max(0, next_t - time.time()))
            behind = int(time.time()) - next_t
            with lock:
                snap = {c: dict(v) for c, v in latest.items()}
            if behind > 2:
                # Fell behind (machine paused, slow loop): fill the missed seconds
                # with the last snapshot, like the forward-filled training grid,
                # so time-based inputs (lags, returns) stay aligned.
                log(f"WARNING: {behind} s behind; forward-filling missed seconds")
                for k in range(behind):
                    tfill = pd.Timestamp(next_t + k, unit="s", tz="UTC")
                    for c in PAIRS:
                        if None not in snap[c].values():
                            hist[c].append((tfill, snap[c]["o"], snap[c]["pmid"], snap[c]["m"], snap[c]["mo"]))
                next_t += behind
            ts = pd.Timestamp(next_t, unit="s", tz="UTC")
            next_t += 1
            today = ts.strftime("%Y-%m-%d")
            if today != day:
                if f:
                    f.close()
                path = out / f"shadow_{today}.csv"
                new = not path.exists()
                f = open(path, "a", newline="")
                w = csv.writer(f)
                if new:
                    w.writerow(["t", "coin", "oracle", "pred_gbm_bp", "pred_base_bp", "prem_dm", "o_stale_s"])
                day = today
            rows_X, meta = [], []
            for c in PAIRS:
                v = snap[c]
                if None in v.values():
                    continue                       # feeds not ready for this market yet
                hist[c].append((ts, v["o"], v["pmid"], v["m"], v["mo"]))
                if len(hist[c]) < 310:
                    continue
                g = pd.DataFrame(list(hist[c]), columns=["t", "o", "pmid", "m", "mo"]).set_index("t")
                feat = compute_features(g, c).iloc[-1]
                if feat[FEATURES].isna().any():
                    continue
                rows_X.append(feat[FEATURES].to_numpy(dtype=float))
                meta.append((c, v["o"], feat["prem_dm"], feat["o_stale_s"]))
            if rows_X:
                X = pd.DataFrame(np.vstack(rows_X), columns=FEATURES)
                p_g = gbm.predict(X)
                p_b = base.predict(X[["prem_dm"]])
                for (c, o, pdm, st), pg, pb in zip(meta, p_g, p_b):
                    w.writerow([next_t - 1, c, o, round(pg, 4), round(pb, 4), round(pdm, 4), int(st)])
                n_pred += len(rows_X)
                f.flush()
            if (next_t % 600) == 0:
                log(f"OK: {n_pred} predictions logged; markets ready "
                    f"{sum(len(h) >= 310 for h in hist.values())}/{len(PAIRS)}")
    except KeyboardInterrupt:
        pass
    finally:
        running = False
        if f:
            f.close()
        log(f"Stopped. {n_pred} predictions logged.")


if __name__ == "__main__":
    main()
