"""Is everything ready for a dex? Read-only, no key needed.

    python check_dex.py --find        # list dexes made by the organizers' deployer
    python check_dex.py newdex        # check one dex (default: HL_DEX or par)

The live phase runs on a NEW dex (name announced late). This shows its
markets, collateral token, leverage and OI caps, compares the markets with
what the bot trades (bot/config.MARKETS) and what ML Model 1 / the mainnet
lead signal cover (bot/features.PAIRS), sanity-checks each oracle against
its mainnet twin, shows the team's balance there, and prints the settings
to use.
"""
import argparse
import os

import requests

from bot import config
from bot.features import PAIRS

TESTNET = "https://api.hyperliquid-testnet.xyz/info"
MAINNET = "https://api.hyperliquid.xyz/info"


def info(url, payload):
    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dex", nargs="?", default=os.environ.get("HL_DEX", "par"))
    ap.add_argument("--find", action="store_true", help="list dexes by the organizers' deployer")
    a = ap.parse_args()

    dexes = {d["name"]: d for d in info(TESTNET, {"type": "perpDexs"}) if d}
    if a.find:
        deployer = dexes["par"]["deployer"]
        print(f"Dexes deployed by {deployer} (par's deployer, the organizers):")
        for d in dexes.values():
            if d["deployer"] == deployer:
                print(f"  {d['name']:12s} {d.get('fullName', '')}  "
                      f"({len(d.get('assetToStreamingOiCap', []))} markets)")
        return

    if a.dex not in dexes:
        raise SystemExit(f"No dex named {a.dex!r} on testnet. Try: python check_dex.py --find")
    d = dexes[a.dex]
    same = d["deployer"] == dexes["par"]["deployer"]
    print(f"Dex {a.dex!r}: {d.get('fullName', '')}  deployer {d['deployer']}"
          + ("  (same deployer as par)" if same else "  (NOT par's deployer: is this the right dex?)"))

    meta, ctxs = info(TESTNET, {"type": "metaAndAssetCtxs", "dex": a.dex})
    tokens = {t["index"]: t["name"] for t in info(TESTNET, {"type": "spotMeta"})["tokens"]}
    coll = tokens.get(meta.get("collateralToken"), "?")
    print(f"Collateral token: {coll} (index {meta.get('collateralToken')})"
          + ("" if coll == config.COLLATERAL else f"   <- differs from HL_COLLATERAL={config.COLLATERAL}"))

    oi_cap = {k: float(v) for k, v in d.get("assetToStreamingOiCap", [])}
    mn = {}
    for dex in ("", "xyz"):
        m, c = info(MAINNET, {"type": "metaAndAssetCtxs", **({"dex": dex} if dex else {})})
        mn.update({u["name"]: float(x["oraclePx"]) for u, x in zip(m["universe"], c) if x.get("oraclePx")})

    markets = []
    print(f"\n{'market':8s} {'oracle':>12s} {'mark':>12s} {'maxLev':>6s} {'szDec':>5s} {'OI cap $M':>9s}  "
          f"{'bot trades':10s} {'ML/mainnet':10s} oracle vs mainnet twin")
    for u, c in zip(meta["universe"], ctxs):
        if u.get("isDelisted"):
            continue
        name = u["name"].split(":", 1)[1]
        markets.append(name)
        o, mk = float(c["oraclePx"]), float(c["markPx"])
        twin = PAIRS.get(name)
        if twin and mn.get(twin):
            diff = (o / mn[twin] - 1) * 1e4
            vs = f"{twin} {mn[twin]:,.6g}  ({diff:+.0f} bp)" + ("  <- CHECK" if abs(diff) > 100 else "")
        else:
            vs = "no twin"
        print(f"{name:8s} {o:12,.6g} {mk:12,.6g} {u['maxLeverage']:6d} {u['szDecimals']:5d} "
              f"{oi_cap.get(u['name'], 0) / 1e6:9,.0f}  {'yes' if name in config.MARKETS else '-':10s} "
              f"{'yes' if twin else 'NO':10s} {vs}")

    missing = [m for m in config.MARKETS if m not in markets]
    new = [m for m in markets if m not in PAIRS]
    print()
    print(f"Bot markets missing on {a.dex}: {missing or 'none'}"
          + ("   <- set MARKETS to the ones that exist" if missing else ""))
    print(f"Markets with no ML model / mainnet twin: {new or 'none'}"
          + ("   <- add to bot/features.PAIRS + retrain before shadow/maker use them" if new else ""))

    spot = info(TESTNET, {"type": "spotClearinghouseState", "user": config.TEAM})
    bal = next((b for b in spot["balances"] if b["coin"] == coll), None)
    st = info(TESTNET, {"type": "clearinghouseState", "user": config.TEAM, "dex": a.dex})
    pos = [p["position"] for p in st.get("assetPositions", []) if float(p["position"]["szi"])]
    print(f"\nTeam {config.TEAM}: {coll} balance {float(bal['total']) if bal else 0:,.2f}; "
          f"open positions on {a.dex}: {len(pos)}")
    for p in pos:
        print(f"  {p['coin']} {p['szi']} @ {p['entryPx']}")

    print("\nSettings for this dex (bot VM ~/run_bot.sh, recorders, shadow, maker):")
    print(f"  export HL_DEX={a.dex}")
    print(f"  export HL_COLLATERAL={coll}")
    if missing:
        print(f"  export MARKETS={','.join(m for m in config.MARKETS if m in markets)}")


if __name__ == "__main__":
    main()
