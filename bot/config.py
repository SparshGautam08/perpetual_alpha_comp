"""All bot settings in one place. Override any of them with an environment
variable of the same name (e.g. LEVERAGE=1.0)."""
import os
from pathlib import Path


def _env(name, default, cast=str):
    v = os.environ.get(name)
    return cast(v) if v not in (None, "") else default


# --- Connection ---
BASE_URL = _env("HL_BASE_URL", "https://api.hyperliquid-testnet.xyz")
MAINNET_URL = "https://api.hyperliquid.xyz"   # outside data: price history
# Live phase moves to a NEW dex (name TBA): set HL_DEX (and HL_COLLATERAL if
# its collateral token differs) everywhere; check first with check_dex.py.
DEX = _env("HL_DEX", "par")
COLLATERAL = _env("HL_COLLATERAL", "USDPR")
TEAM = _env("HL_TEAM_ADDRESS", "0xC9f1C5a784d2cd5858396299893e71cfe23F4aAf")
KEY_FILE = _env("HL_API_KEY_FILE", "")         # only needed in live mode

# --- Markets ---
# CLEAN7 from the backtests: no thin FX/EWJ (stale prices, costly funding).
MARKETS = _env("MARKETS", "BTC,ETH,HYPE,SP500,EWY,BRENT,GOLD").split(",")
# par market -> Hyperliquid mainnet market it copies (historical/README.md)
MAINNET_COIN = {"BTC": "BTC", "ETH": "ETH", "HYPE": "HYPE", "SP500": "xyz:SP500",
                "EWY": "xyz:EWY", "EWJ": "xyz:EWJ", "BRENT": "xyz:BRENTOIL",
                "GOLD": "xyz:GOLD", "USDEUR": "xyz:EUR", "USDJPY": "xyz:JPY"}

# --- Strategy: risk parity, sized by a volatility target (REPORT_NOTES E5) ---
VOL_TARGET = _env("VOL_TARGET", 0.10, float)    # annualized, before leverage
LEVERAGE = _env("LEVERAGE", 0.5, float)         # 0.5 x 10% = 5% target vol
HISTORY_DAYS = _env("HISTORY_DAYS", 40, int)    # mainnet hourly candles used
REBAL_HOUR_UTC = _env("REBAL_HOUR_UTC", 0, int)  # rebalance once a day at this hour
BAND = _env("BAND", 0.01, float)                # skip trades < 1% of equity

# --- Risk limits ---
MAX_GROSS = _env("MAX_GROSS", 1.5, float)          # sum |weights| cap
MAX_WEIGHT = _env("MAX_WEIGHT", 0.30, float)       # per-market |weight| cap
DAILY_LOSS_STOP = _env("DAILY_LOSS_STOP", 0.02, float)  # flatten at -2% on the day
MAX_SPREAD_BP = _env("MAX_SPREAD_BP", 100, float)  # skip a market if book looks broken

# --- Start gate ---
# No orders before TRADE_FROM (ISO UTC; empty = no gate) or while equity is 0
# (dex not funded yet), so the bot can be switched to the live dex early.
TRADE_FROM = _env("TRADE_FROM", "")

# --- Eligibility safeguard (bot/eligibility.py) ---
# >= 1 fill in the scored phase is needed to be ranked. If ELIG_TRADE_BY
# (ISO UTC, e.g. 2026-10-20T20:00:00Z) has passed and the account has no
# fill on DEX since SCORED_START, make one ~ELIG_USD round trip. Empty = off.
SCORED_START = _env("SCORED_START", "2026-10-19T12:00:00Z")
ELIG_TRADE_BY = _env("ELIG_TRADE_BY", "")
ELIG_USD = _env("ELIG_USD", 15.0, float)

# --- ML filter for drift entries (REPORT_NOTES E39) ---
# yes = judge drift entries on the edge adjusted by ML Model 1's predicted
# 15 s oracle move, read from shadow mode's log on the same machine.
ML_FILTER = _env("DRIFT_ML_FILTER", "no").lower() == "yes"
SHADOW_DIR = _env("SHADOW_DIR", "~/shadow")
ML_MAX_AGE_S = _env("ML_MAX_AGE_S", 5, float)   # older predictions are ignored

# --- Execution ---
MAX_ORDER_USD = _env("MAX_ORDER_USD", 25_000, float)  # slice bigger trades
# Worst accepted price vs the MID (not the best quote: tiny orders sometimes
# sit in front of the bot's $50k level). Bot level 1 is ~10 bp from mid.
MAX_TAKE_BP = _env("MAX_TAKE_BP", 15, float)
MIN_ORDER_USD = 11.0                                  # exchange minimum is $10
TAKER_FEE_BP = 9.0                                    # measured Oct 7 (paper fills)

# --- Loop, files, alerts ---
LOOP_SECONDS = _env("LOOP_SECONDS", 60, int)
ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(_env("BOT_STATE_DIR", str(ROOT / "state")))
STOP_FILE = STATE_DIR / "STOP"     # create it: flatten everything and exit
PAUSE_FILE = STATE_DIR / "PAUSE"   # create it: keep running, place no orders
REBALANCE_FILE = STATE_DIR / "REBALANCE"  # create it: rebalance on next loop (then deleted)
NTFY_TOPIC = _env("NTFY_TOPIC", "")  # secret-ish topic name; empty = log only
