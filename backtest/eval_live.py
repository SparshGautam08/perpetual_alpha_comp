"""E42/E50: evaluation of the scored phase, from public on-chain data only.

    .venv/bin/python -m backtest.eval_live

Fills (userFillsByTime), funding (userFunding), equity (portfolio) and the
official row (leaderboard.json). Splits P&L into carry and drift, reports H1
(drift round trips, bootstrap CI), the carry criterion of E46 (funding vs
price + costs) and H3 (daily std, drawdown). H2, H4 and H5 need the bot VM's
recordings and logs: backtest.pas_drift, backtest.eval_shadow, bot.log.
"""
import calendar
import time
from collections import defaultdict

import numpy as np
import requests

URL = "https://api.hyperliquid-testnet.xyz/info"
BOARD = "https://d23m2l9bjnbgul.cloudfront.net/public/leaderboard.json"
TEAM = "0xC9f1C5a784d2cd5858396299893e71cfe23F4aAf"
TEAM_NAME = "SAPS"
START_MS = calendar.timegm((2026, 10, 19, 12, 0, 0)) * 1000
BANKROLL = 100_000.0
# Drift's only SP500 trade, on top of the carry long (E47): entry and exit
# fills fall in these windows, UTC ms; all other SP500 fills are carry.
DRIFT_SP500 = (calendar.timegm((2026, 10, 21, 19, 50, 0)) * 1000, calendar.timegm((2026, 10, 21, 19, 54, 0)) * 1000,
               calendar.timegm((2026, 10, 22, 7, 50, 0)) * 1000, calendar.timegm((2026, 10, 22, 7, 54, 0)) * 1000)


def post(body):
    r = requests.post(URL, json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def paged(kind, key_full=2000):
    out, t = [], START_MS
    while True:
        rows = post({"type": kind, "user": TEAM, "startTime": t})
        if not rows:
            return out
        out += rows
        if len(rows) < key_full:
            return out
        t = rows[-1]["time"] + 1


def stamp(ms):
    return time.strftime("%m-%d %H:%M", time.gmtime(ms / 1000))


def is_drift_sp500(ms):
    a, b, c, d = DRIFT_SP500
    return a <= ms <= b or c <= ms <= d


def round_trips(fills):
    """Zero-to-zero trips per coin; returns (closed trips, open legs)."""
    pos, cur, trips = defaultdict(float), {}, []
    for f in fills:
        c, sz, px = f["coin"], float(f["sz"]), float(f["px"])
        s = sz if f["side"] == "B" else -sz
        if abs(pos[c]) < 1e-9:
            cur[c] = dict(coin=c.split(":")[1], t0=f["time"], t1=None, side="long" if s > 0 else "short",
                          price=0.0, fee=0.0, max_usd=0.0, funding=0.0)
        tr = cur[c]
        tr["price"] += float(f["closedPnl"])
        tr["fee"] += float(f["fee"])
        pos[c] += s
        tr["max_usd"] = max(tr["max_usd"], abs(pos[c]) * px)
        tr["t1"] = f["time"]
        if abs(pos[c]) < 1e-6:
            trips.append(tr)
            pos[c] = 0.0
            del cur[c]
    return trips, cur


def official_q(R, sigma):
    return (max(R, 0) + 0.005) / (abs(R) + 1.5 * sigma + 0.05)


def main():
    fills = sorted((f for f in paged("userFillsByTime") if f["coin"].startswith("pas:")), key=lambda f: f["time"])
    funding = [x for x in paged("userFunding", 500) if x["delta"]["coin"].startswith("pas:")]
    state = post({"type": "clearinghouseState", "user": TEAM, "dex": "pas"})

    # drift's SP500 trade, then everything else by coin
    dr_sp = [f for f in fills if f["coin"] == "pas:SP500" and is_drift_sp500(f["time"])]
    rest = [f for f in fills if f not in dr_sp]
    carry_fills = [f for f in rest if f["coin"] == "pas:SP500"]
    trips, _ = round_trips([f for f in rest if f["coin"] != "pas:SP500"])

    if dr_sp:
        buys = [f for f in dr_sp if f["side"] == "B"]
        sells = [f for f in dr_sp if f["side"] == "A"]
        units = sum(float(f["sz"]) for f in buys)
        price = sum(float(f["sz"]) * float(f["px"]) for f in sells) - sum(float(f["sz"]) * float(f["px"]) for f in buys)
        trips.append(dict(coin="SP500", t0=buys[0]["time"], t1=sells[-1]["time"], side="long", price=price,
                          fee=sum(float(f["fee"]) for f in dr_sp), max_usd=units * float(buys[0]["px"]), funding=0.0,
                          units=units))
    trips.sort(key=lambda t: t["t0"])

    # funding: non-SP500 goes to the trip holding it; SP500 by share of size
    fund_carry = fund_total = 0.0
    for x in funding:
        d, t, usd = x["delta"], x["time"], float(x["delta"]["usdc"])
        fund_total += usd
        coin = d["coin"].split(":")[1]
        held = [tr for tr in trips if tr["coin"] == coin and tr["t0"] <= t <= tr["t1"]]
        if coin == "SP500":
            tr = held[0] if held else None
            if tr:
                share = tr["units"] / abs(float(d["szi"]))
                tr["funding"] += usd * share
                usd *= 1 - share
            fund_carry += usd
        elif held:
            held[0]["funding"] += usd
    for tr in trips:
        tr["net"] = tr["price"] - tr["fee"] + tr["funding"]

    print(f"Drift round trips (H1), {len(trips)}:")
    for tr in trips:
        print(f"  {stamp(tr['t0'])} -> {stamp(tr['t1'])} {tr['coin']:7}{tr['side']:6} max ${tr['max_usd']:>7,.0f}  "
              f"price {tr['price']:+8.2f} fees {tr['fee']:6.2f} funding {tr['funding']:+7.2f} net {tr['net']:+8.2f}")
    nets = np.array([tr["net"] for tr in trips])
    rng = np.random.default_rng(42)
    boot = rng.choice(nets, size=(10_000, len(nets))).mean(axis=1)
    print(f"  total {nets.sum():+,.2f}; mean per trip {nets.mean():+.2f}; 95% bootstrap CI "
          f"[{np.quantile(boot, .025):+.2f}, {np.quantile(boot, .975):+.2f}]; wins {(nets > 0).sum()}/{len(nets)}")

    # carry: all SP500 P&L minus drift's trade (the exchange books closes at
    # average cost, so drift's own fills give its true price P&L); split into
    # episodes at each time the carry was flat
    upnl = sum(float(p["position"]["unrealizedPnl"]) for p in state["assetPositions"])
    dr_price = sum(tr["price"] for tr in trips if tr["coin"] == "SP500")
    sp_closed = sum(float(f["closedPnl"]) for f in fills if f["coin"] == "pas:SP500")
    c_fee = sum(float(f["fee"]) for f in carry_fills)
    c_price = sp_closed - dr_price
    print(f"\nCarry (E46 criterion: funding > price + costs): funding {fund_carry:+,.2f}, realized price "
          f"{c_price:+,.2f}, open price at mark {upnl:+,.2f}, fees -{c_fee:,.2f}, "
          f"net {fund_carry + c_price + upnl - c_fee:+,.2f}")
    closes = [f["time"] for f in carry_fills if f["dir"].startswith("Close")]
    if closes and carry_fills[-1]["dir"].startswith("Open"):
        cut = max(closes)
        f1 = sum(float(x["delta"]["usdc"]) for x in funding if x["delta"]["coin"] == "pas:SP500" and x["time"] <= cut)
        f1 -= sum(tr["funding"] for tr in trips if tr["coin"] == "SP500")
        fee1 = sum(float(f["fee"]) for f in carry_fills if f["time"] <= cut)
        print(f"  episode 1 (to {stamp(cut)}): funding {f1:+,.2f}, price {c_price:+,.2f}, fees -{fee1:,.2f}, "
              f"net {f1 + c_price - fee1:+,.2f}")
        print(f"  episode 2 (open): funding {fund_carry - f1:+,.2f}, price at mark {upnl:+,.2f}, fees -{c_fee - fee1:,.2f}, "
              f"net {fund_carry - f1 + upnl - (c_fee - fee1):+,.2f}")
    for p in state["assetPositions"]:
        q = p["position"]
        print(f"  open: {q['coin']} {q['szi']} entry {q['entryPx']}, uPnL at mark {float(q['unrealizedPnl']):+.2f}")

    # reconciliation with the account
    total = sum(float(f["closedPnl"]) - float(f["fee"]) for f in fills) + fund_total + upnl
    print(f"\nAll fills + funding + open P&L = {total:+,.2f}  ->  implied equity {BANKROLL + total:,.2f}")
    print(f"Fees total {sum(float(f['fee']) for f in fills):,.2f} on {len(fills)} fills; "
          f"volume ${sum(float(f['sz']) * float(f['px']) for f in fills):,.0f}")

    # official row
    lb = requests.get(BOARD, timeout=30).json()
    ranked = sorted((x for x in lb["teams"] if x["ranked"]), key=lambda x: x["rank"])
    me = next(x for x in ranked if x["name"] == TEAM_NAME)
    print(f"\nLeaderboard {lb['generated_at'][:16]} UTC: {TEAM_NAME} #{me['rank']} of {len(ranked)}, R "
          f"{100 * me['return']:+.3f}%, daily std {100 * me['volatility']:.3f}%, sigma "
          f"{100 * me['volatility_horizon']:.3f}%, Q {me['q']:.3f}; {sum(x['return'] > 0 for x in ranked)} teams positive")
    print("H3 daily std <= 0.5%: " + ("PASS" if me["volatility"] <= 0.005 else "FAIL") + " (official, at mark)")

    # H3: equity at each 12:00 UTC, rebuilt from fills, funding and hourly prices
    samples = daily_equity(fills, funding)
    v = np.array([x[1] for x in samples])
    r = v[1:] / v[:-1] - 1
    dd = v / np.maximum.accumulate(v) - 1
    print(f"\nRebuilt 12:00 UTC equity ({len(v)} samples; last trade before each hour's open, so close to, not "
          f"equal to, the official mark): daily std {100 * r.std(ddof=1):.3f}%, max drawdown {100 * dd.min():.2f}% "
          f"(H3 limit 3%), worst day {100 * r.min():+.2f}%, best day {100 * r.max():+.2f}%")
    for (t, x) in samples:
        print(f"  {stamp(t)} {x:>11,.2f}  {100 * (x / BANKROLL - 1):+.2f}%")


def daily_equity(fills, funding):
    day = 86_400_000
    now = int(time.time() * 1000)
    closes = {}
    for coin in {f["coin"] for f in fills}:
        rows = post({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h",
                                                       "startTime": START_MS - 3_600_000, "endTime": now}})
        closes[coin] = sorted((c["t"], float(c["o"]), float(c["c"])) for c in rows)

    def price_at(coin, t):
        px = None
        for t0, o, c in closes[coin]:
            if t0 > t:
                break
            px = o if t0 == t else c
        return px

    out = []
    for t in range(START_MS, now, day):
        cash, pos = BANKROLL, defaultdict(float)
        for f in fills:
            if f["time"] > t:
                break
            s = float(f["sz"]) * (1 if f["side"] == "B" else -1)
            pos[f["coin"]] += s
            cash -= s * float(f["px"]) + float(f["fee"])
        cash += sum(float(x["delta"]["usdc"]) for x in funding if x["time"] <= t)
        value = sum(q * price_at(c, t) for c, q in pos.items() if abs(q) > 1e-9)
        out.append((t, cash + value))
    return out


if __name__ == "__main__":
    main()
