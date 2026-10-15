"""Latest ML Model 1 predictions for the live bot, read from shadow mode's log.

Shadow mode (bot/shadow.py) runs on the same VM and appends one row per
market per second to <SHADOW_DIR>/shadow_<UTC date>.csv (flushed every
second): t, coin, oracle, pred_gbm_bp, ... Reading its tail costs the bot
no extra websockets, memory or model copy. A prediction older than
ML_MAX_AGE_S (shadow stopped, lagging, or the day just rolled over) is
ignored, so the bot falls back to plain drift.
"""
import csv
import io
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

TAIL_BYTES = 16_384          # ~20 s of rows for 10 markets


def latest_predictions(now_s: float | None = None) -> dict[str, float]:
    """{coin: predicted oracle move over the next 15 s, bp}, fresh rows only."""
    now_s = now_s or time.time()
    day = datetime.fromtimestamp(now_s, timezone.utc).strftime("%Y-%m-%d")
    path = Path(config.SHADOW_DIR).expanduser() / f"shadow_{day}.csv"
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return {}
    lines = tail.splitlines()
    if size > TAIL_BYTES:
        lines = lines[1:]                     # first line is probably cut
    out, newest = {}, {}
    for row in csv.reader(io.StringIO("\n".join(lines))):
        if len(row) < 4 or row[0] == "t":
            continue
        try:
            t, coin, pred = int(row[0]), row[1], float(row[3])
        except ValueError:
            continue
        if now_s - t <= config.ML_MAX_AGE_S and t >= newest.get(coin, 0):
            newest[coin], out[coin] = t, pred
    return out
