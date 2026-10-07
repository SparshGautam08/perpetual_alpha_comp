"""Show the team's balance and open positions on par. Read-only, no key needed.

    python balance.py
"""
import os

import requests

BASE_URL = os.environ.get("HL_BASE_URL", "https://api.hyperliquid-testnet.xyz")
TEAM = os.environ.get("HL_TEAM_ADDRESS", "0xC9f1C5a784d2cd5858396299893e71cfe23F4aAf")
DEX = "par"


def info(payload):
    r = requests.post(BASE_URL + "/info", json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


spot = info({"type": "spotClearinghouseState", "user": TEAM})
usdpr = next((b for b in spot["balances"] if b["coin"] == "USDPR"), None)
perp = info({"type": "clearinghouseState", "user": TEAM, "dex": DEX})
meta, ctxs = info({"type": "metaAndAssetCtxs", "dex": DEX})
oracle = {a["name"]: float(c["oraclePx"]) for a, c in zip(meta["universe"], ctxs)}

cash = float(usdpr["total"]) if usdpr else 0.0
print(f"USDPR balance: {cash:,.2f}  (held as margin: {float(usdpr['hold']) if usdpr else 0:,.2f})")

positions = [p["position"] for p in perp.get("assetPositions", [])]
upnl_mark = upnl_oracle = 0.0
if positions:
    print(f"\n{'market':12s}{'size':>12s}{'entry':>12s}{'oracle':>12s}{'value $':>12s}"
          f"{'uPnL@mark':>12s}{'uPnL@oracle':>13s}{'funding':>10s}")
    for p in positions:
        szi, entry = float(p["szi"]), float(p["entryPx"])
        o = oracle[p["coin"]]
        u_o = szi * (o - entry)
        upnl_mark += float(p["unrealizedPnl"])
        upnl_oracle += u_o
        print(f"{p['coin']:12s}{szi:>12g}{entry:>12g}{o:>12g}{abs(szi) * o:>12,.2f}"
              f"{float(p['unrealizedPnl']):>12,.2f}{u_o:>13,.2f}"
              f"{-float(p['cumFunding']['sinceOpen']):>10,.4f}")
else:
    print("No open positions on par.")

# Unified account: the USDPR total already includes unrealized P&L at mark
# (checked Oct 7: 1,000,000 - 0.90 fee - 0.64 uPnL = 999,998.46).
print(f"\nEquity at mark:   {cash:,.2f}")
print(f"Equity at oracle: {cash - upnl_mark + upnl_oracle:,.2f}   (what we're scored on)")
