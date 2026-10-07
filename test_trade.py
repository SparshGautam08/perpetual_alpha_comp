"""Small live test trade on par, to confirm what recordings can't show:
the bot can trade, our real fee rate, that funding is actually paid, and
how fills compare to the oracle.

Steps (run in order; nothing trades without --confirm):
  python test_trade.py plan  --coin BTC --usd 1000            # read-only preview
  python test_trade.py open  --coin BTC --usd 1000 --confirm  # buy, IoC at best ask
  python test_trade.py status --coin BTC                      # position, funding so far
  python test_trade.py close --coin BTC --confirm             # sell everything back
  python test_trade.py report --coin BTC                      # fees, funding, P&L vs oracle

Safety:
- Hard cap MAX_USD per test. Orders are immediate-or-cancel limit orders
  priced at the best quote plus at most MAX_SLIP_BP, so a thin book can't
  fill us far from the mid; any unfilled part is cancelled.
- open/close need --confirm. plan/status/report are read-only and need no key.
- The key is read from HL_API_KEY_FILE and never printed.
"""
import argparse
import json
import os
import time

import requests

BASE_URL = os.environ.get("HL_BASE_URL", "https://api.hyperliquid-testnet.xyz")
DEX = "par"
TEAM = os.environ.get("HL_TEAM_ADDRESS", "0xC9f1C5a784d2cd5858396299893e71cfe23F4aAf")
MAX_USD = 2_000          # refuse anything bigger
MAX_SLIP_BP = 5          # worst price = best quote +/- this many bp
LOG = "test_trade_log.jsonl"   # every order response, for the report


def info(payload):
    r = requests.post(BASE_URL + "/info", json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


def market(coin):
    """Oracle, mid, best bid/ask and size decimals for par:<coin>."""
    name = f"{DEX}:{coin}"
    meta, ctxs = info({"type": "metaAndAssetCtxs", "dex": DEX})
    i = [a["name"] for a in meta["universe"]].index(name)
    book = info({"type": "l2Book", "coin": name})["levels"]
    bid, ask = book[0][0], book[1][0]
    return {"name": name, "szDecimals": meta["universe"][i]["szDecimals"],
            "oracle": float(ctxs[i]["oraclePx"]), "mark": float(ctxs[i]["markPx"]),
            "funding_hourly": float(ctxs[i]["funding"]),
            "bid": float(bid["px"]), "bid_usd": float(bid["px"]) * float(bid["sz"]),
            "ask": float(ask["px"]), "ask_usd": float(ask["px"]) * float(ask["sz"])}


def round_px(px, sz_decimals):
    """Hyperliquid perps: 5 significant figures, at most 6 - szDecimals decimals."""
    return round(float(f"{px:.5g}"), 6 - sz_decimals)


def position(name):
    st = info({"type": "clearinghouseState", "user": TEAM, "dex": DEX})
    for p in st.get("assetPositions", []):
        if p["position"]["coin"] == name:
            return p["position"]
    return None


def exchange():
    import eth_account
    from hyperliquid.exchange import Exchange
    with open(os.path.expanduser(os.environ["HL_API_KEY_FILE"])) as f:
        wallet = eth_account.Account.from_key(f.read().strip())
    return Exchange(wallet, BASE_URL, account_address=TEAM, perp_dexs=[DEX])


def log_event(kind, data):
    with open(LOG, "a") as f:
        f.write(json.dumps({"t": int(time.time() * 1000), "kind": kind, **data}) + "\n")


def cmd_plan(a):
    m = market(a.coin)
    sz = round(a.usd / m["ask"], m["szDecimals"])
    px = round_px(m["ask"] * (1 + MAX_SLIP_BP / 1e4), m["szDecimals"])
    print(f"Market {m['name']}: oracle {m['oracle']}, mark {m['mark']}, "
          f"bid {m['bid']} (${m['bid_usd']:,.0f}), ask {m['ask']} (${m['ask_usd']:,.0f})")
    print(f"Spread {(m['ask'] / m['bid'] - 1) * 1e4:.1f} bp, ask vs oracle "
          f"{(m['ask'] / m['oracle'] - 1) * 1e4:+.1f} bp, funding "
          f"{m['funding_hourly'] * 24 * 365 * 100:+.1f}%/yr (longs pay if +)")
    print(f"Plan: BUY {sz} {m['name']} (~${sz * m['ask']:,.0f}), IoC limit {px} "
          f"(best ask + {MAX_SLIP_BP} bp max)")
    print(f"Est. round-trip cost: ~${sz * m['ask'] * (m['ask'] / m['bid'] - 1 + 2 * 5e-4):.2f} "
          "(spread + 2 x ~5 bp fee, fee unconfirmed)")
    if sz * m["ask"] > m["ask_usd"]:
        print("WARNING: bigger than the best ask level; part may not fill.")


def cmd_open(a):
    if a.usd > MAX_USD:
        raise SystemExit(f"Refusing: ${a.usd} > MAX_USD ${MAX_USD}")
    if not a.confirm:
        raise SystemExit("Add --confirm to place the order (run 'plan' first).")
    m = market(a.coin)
    sz = round(a.usd / m["ask"], m["szDecimals"])
    px = round_px(m["ask"] * (1 + MAX_SLIP_BP / 1e4), m["szDecimals"])
    log_event("pre_open", {"market": m})
    res = exchange().order(m["name"], True, sz, px, {"limit": {"tif": "Ioc"}})
    log_event("open", {"request": {"sz": sz, "px": px}, "response": res})
    print(json.dumps(res, indent=2))


def cmd_close(a):
    if not a.confirm:
        raise SystemExit("Add --confirm to close.")
    m = market(a.coin)
    p = position(m["name"])
    if not p or float(p["szi"]) == 0:
        raise SystemExit("No open position.")
    szi = float(p["szi"])
    is_buy = szi < 0
    ref = m["ask"] if is_buy else m["bid"]
    px = round_px(ref * (1 + MAX_SLIP_BP / 1e4) if is_buy else ref * (1 - MAX_SLIP_BP / 1e4),
                  m["szDecimals"])
    log_event("pre_close", {"market": m, "position": p})
    res = exchange().order(m["name"], is_buy, abs(szi), px, {"limit": {"tif": "Ioc"}},
                           reduce_only=True)
    log_event("close", {"request": {"sz": abs(szi), "px": px}, "response": res})
    print(json.dumps(res, indent=2))


def first_open_ms():
    try:
        with open(LOG) as f:
            for line in f:
                e = json.loads(line)
                if e["kind"] == "pre_open":
                    return e["t"]
    except FileNotFoundError:
        pass
    return int(time.time() * 1000) - 7 * 86400 * 1000


def cmd_status(a):
    name = f"{DEX}:{a.coin}"
    print("Position:", json.dumps(position(name), indent=2))
    since = first_open_ms()
    fund = [f for f in info({"type": "userFunding", "user": TEAM, "startTime": since})
            if f.get("delta", {}).get("coin") == name]
    total = sum(float(f["delta"]["usdc"]) for f in fund)
    print(f"Funding events since open: {len(fund)}, total {total:+.6f} (negative = we paid)")
    for f in fund[-5:]:
        d = f["delta"]
        print(f"  {time.strftime('%H:%M', time.gmtime(f['time'] / 1000))}Z "
              f"rate {float(d['fundingRate']):+.7f}/h  paid {float(d['usdc']):+.6f}  szi {d['szi']}")


def cmd_report(a):
    name = f"{DEX}:{a.coin}"
    since = first_open_ms()
    fills = [f for f in info({"type": "userFills", "user": TEAM})
             if f["coin"] == name and f["time"] >= since]
    oracle_at = {}
    with open(LOG) as f:
        for line in f:
            e = json.loads(line)
            if e["kind"] in ("pre_open", "pre_close"):
                oracle_at[e["kind"]] = e["market"]["oracle"]
    print(f"Fills: {len(fills)}")
    notional = fees = 0.0
    for f in fills:
        n = float(f["px"]) * float(f["sz"])
        notional += n
        fees += float(f["fee"])
        side = "BUY " if f["side"] == "B" else "SELL"
        ref = oracle_at.get("pre_open" if side == "BUY " else "pre_close")
        vs = f"  vs oracle {(float(f['px']) / ref - 1) * 1e4:+.1f} bp" if ref else ""
        print(f"  {side} {f['sz']} @ {f['px']}  ${n:,.2f}  fee {f['fee']} {f.get('feeToken', '')}"
              f"  crossed={f.get('crossed')}{vs}  closedPnl {f.get('closedPnl')}")
    if notional:
        print(f"Fee rate: {fees / notional * 1e4:.2f} bp of notional")
    fund = [x for x in info({"type": "userFunding", "user": TEAM, "startTime": since})
            if x.get("delta", {}).get("coin") == name]
    ftot = sum(float(x["delta"]["usdc"]) for x in fund)
    print(f"Funding: {len(fund)} payments, total {ftot:+.6f}")
    pnl = sum(float(f.get("closedPnl", 0)) for f in fills)
    print(f"Closed P&L (price only) {pnl:+.4f}, fees -{fees:.4f}, funding {ftot:+.6f}, "
          f"net {pnl - fees + ftot:+.4f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan", "open", "status", "close", "report"])
    ap.add_argument("--coin", default="BTC")
    ap.add_argument("--usd", type=float, default=1000)
    ap.add_argument("--confirm", action="store_true")
    a = ap.parse_args()
    {"plan": cmd_plan, "open": cmd_open, "status": cmd_status,
     "close": cmd_close, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    main()
