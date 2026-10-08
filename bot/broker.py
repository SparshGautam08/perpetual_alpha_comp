"""Brokers: the only place orders happen.

PaperBroker simulates fills against the live best quote (taker fee, hourly
funding) and saves its book to state/paper.json. LiveBroker sends real
orders to par. Both expose the same methods, so the rest of the bot can't
tell them apart."""
import json
import logging
import os
import time

from . import config
from .market import Quote, post

log = logging.getLogger("bot")


def round_px(px: float, sz_decimals: int) -> float:
    """Hyperliquid perps: 5 significant figures, at most 6 - szDecimals decimals."""
    return round(float(f"{px:.5g}"), 6 - sz_decimals)


class PaperBroker:
    mode = "paper"

    def __init__(self, start_equity: float = 1_000_000.0, name: str = "paper"):
        self.path = config.STATE_DIR / f"{name}.json"
        if self.path.exists():
            s = json.loads(self.path.read_text())
        else:
            s = {"cash": start_equity, "pos": {}, "entry": {}, "last_funding_hour": None,
                 "fees": 0.0, "funding": 0.0}
        self.s = s

    def _save(self):
        self.path.write_text(json.dumps(self.s, indent=2))

    def positions(self) -> dict[str, float]:
        return {k: v for k, v in self.s["pos"].items() if v != 0}

    def equity_oracle(self, quotes: dict[str, Quote]) -> float:
        """Cash already includes realized P&L; add open P&L at oracle."""
        upnl = sum(sz * (quotes[c].oracle - self.s["entry"][c])
                   for c, sz in self.positions().items() if c in quotes)
        return self.s["cash"] + upnl

    def accrue_funding(self, quotes: dict[str, Quote]):
        """Charge funding once per hour, like Hyperliquid (+ rate: longs pay)."""
        hour = int(time.time() // 3600)
        if self.s["last_funding_hour"] == hour:
            return
        if self.s["last_funding_hour"] is not None:
            for c, sz in self.positions().items():
                pay = sz * quotes[c].oracle * quotes[c].funding
                self.s["cash"] -= pay
                self.s["funding"] -= pay
        self.s["last_funding_hour"] = hour
        self._save()

    def order(self, q: Quote, is_buy: bool, sz: float, limit_px: float,
              reduce_only: bool) -> tuple[float, float]:
        """Immediate-or-cancel: take every book level priced within the
        limit, up to sz; the rest is cancelled. Returns (filled, avg px)."""
        cur = self.s["pos"].get(q.coin, 0.0)
        if reduce_only:
            sz = min(sz, abs(cur))
        want, got, cost = sz, 0.0, 0.0
        for lpx, lsz in (q.asks if is_buy else q.bids) or []:
            if (is_buy and lpx > limit_px) or (not is_buy and lpx < limit_px):
                break
            take = min(lsz, want - got)
            got += take
            cost += take * lpx
            if got >= want:
                break
        sz = round(got, q.sz_decimals)
        if sz <= 0:
            return 0.0, 0.0
        px = cost / got
        d = sz if is_buy else -sz
        new = cur + d
        entry = self.s["entry"].get(q.coin, px)
        if cur == 0 or (cur > 0) == (d > 0):            # opening / adding
            entry = (abs(cur) * entry + sz * px) / (abs(cur) + sz)
        else:                                           # reducing / flipping
            closed = min(abs(d), abs(cur))
            self.s["cash"] += closed * (px - entry) * (1 if cur > 0 else -1)
            if abs(d) > abs(cur):
                entry = px
        fee = sz * px * config.TAKER_FEE_BP / 1e4
        self.s["cash"] -= fee
        self.s["fees"] += fee
        self.s["pos"][q.coin] = round(new, 10)
        self.s["entry"][q.coin] = entry
        self._save()
        return sz, px


class LiveBroker:
    mode = "live"

    def __init__(self):
        import eth_account
        from hyperliquid.exchange import Exchange
        with open(os.path.expanduser(config.KEY_FILE)) as f:
            wallet = eth_account.Account.from_key(f.read().strip())
        self.ex = Exchange(wallet, config.BASE_URL, account_address=config.TEAM,
                           perp_dexs=[config.DEX])

    def _state(self):
        return post(config.BASE_URL, {"type": "clearinghouseState",
                                      "user": config.TEAM, "dex": config.DEX})

    def positions(self) -> dict[str, float]:
        out = {}
        for p in self._state().get("assetPositions", []):
            coin = p["position"]["coin"].split(":", 1)[1]
            if float(p["position"]["szi"]) != 0:
                out[coin] = float(p["position"]["szi"])
        return out

    def equity_oracle(self, quotes: dict[str, Quote]) -> float:
        """Unified account: USDPR total already includes uPnL at mark
        (REPORT_NOTES E10), so swap mark uPnL for oracle uPnL."""
        spot = post(config.BASE_URL, {"type": "spotClearinghouseState", "user": config.TEAM})
        total = float(next(b["total"] for b in spot["balances"] if b["coin"] == "USDPR"))
        adj = 0.0
        for p in self._state().get("assetPositions", []):
            pos = p["position"]
            coin = pos["coin"].split(":", 1)[1]
            o = quotes[coin].oracle if coin in quotes else float(pos["entryPx"])
            adj += float(pos["szi"]) * (o - float(pos["entryPx"])) - float(pos["unrealizedPnl"])
        return total + adj

    def accrue_funding(self, quotes):
        pass    # the exchange pays/charges funding itself

    def order(self, q: Quote, is_buy: bool, sz: float, limit_px: float,
              reduce_only: bool) -> tuple[float, float]:
        res = self.ex.order(q.name, is_buy, sz, limit_px, {"limit": {"tif": "Ioc"}},
                            reduce_only=reduce_only)
        if res.get("status") != "ok":
            raise RuntimeError(f"order rejected: {res}")
        st = res["response"]["data"]["statuses"][0]
        if "filled" in st:
            return float(st["filled"]["totalSz"]), float(st["filled"]["avgPx"])
        if "error" in st and "could not immediately match" not in st["error"].lower():
            raise RuntimeError(f"order error: {st['error']}")
        return 0.0, 0.0
