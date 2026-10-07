# Report notes: experiment log

Every experiment we run, in order: what we tried, how to reproduce it, the
results, and what we learned, including failures and claims we dropped.
This is the raw material for the 2-4 page technical report.

Conventions:
- In-sample (IS) = 2026-04-18 to 2026-07-31; out-of-sample (OOS) =
  2026-08-01 to 2026-10-05. Settings are chosen on IS only.
- "Score" = 40 x normalized return + 30 x normalized low daily std (out of 70;
  the report's 30% can't be backtested). See `backtest/engine.py: score()`.
- Costs unless stated: fee 5 bp/side, half-spread 10 bp, funding from
  `backtest/costs.py` (provisional constants).
- Raw outputs go to `results/` (not in git); rerun the command to recreate.

---

## E1. Historical dataset audit (Oct 6)
**Tried:** loaded the organizers' 10 hourly candle files and checked coverage
and quality. **Reproduce:** `python -m backtest.dataset_summary`

**Results**
- Hourly mainnet trade candles, 2026-03-11 to 2026-10-05 15:00 UTC, ~5,000
  rows per market (SP500 starts 2026-03-18). Capped by Hyperliquid's
  5,000-candle limit.
- No missing hours, duplicates, nulls, non-positive prices or broken OHLC.
- Thin markets: EWJ 257 zero-trade rows (9.7% flat hours), USDEUR 86,
  USDJPY 51, EWY 2. Clean: BTC, ETH, HYPE, SP500, GOLD, BRENT.
- Weekends: equities, GOLD and BRENT trade at 20-32% of weekday volume;
  quiet windows are mainly Saturday and early Sunday UTC. Closures do not
  show up as missing rows because mainnet perps trade 24/7.
- Correlations (daily, Apr 18 - Oct 5): BTC-ETH 0.88; BRENT and USDJPY are
  negatively correlated with equities and crypto (-0.15 to -0.53).

**Learned / problems found**
- README is wrong in two places: it says hours with no trades have no row
  (they exist, with trades = 0), and it names markets `pa:` (live API uses
  `par:`). We treat trades = 0 as missing and forward-fill the price.
- USDEUR is quoted as USD per EUR (~1.15). Need to confirm the oracle uses
  the same direction.
- Stale prices in thin markets fake low volatility and later "catch-up"
  moves; any signal on EWJ/FX must use cleaned returns.

## E2. Baselines v0, low costs (Oct 6)
**Tried:** buy-and-hold, equal weight (daily rebalance), risk parity
(inverse-vol, EWMA cov, 10% and 20% vol targets), time-series trend
(long/short and long-only, lookbacks 7d + 30d). Costs 5 bp fee + 2 bp slip,
no funding. **Reproduce:** `git checkout e074760 && python -m backtest.run_baselines`

**Results (ALL10)**
| Strategy | IS return | OOS return | Daily std |
|---|---|---|---|
| Buy-and-hold | -2.8% | +20.5% | 1.1-1.3% |
| Risk parity 10% vol | -3.0% | +7.4% | 0.55-0.6% |
| Trend long/short | -0.9% | +4.1% | 0.55-0.7% |

**Learned**
- Market direction dominates: every long-biased strategy looks bad IS
  (flat/down market) and good OOS (rally). Being long is a bet, not an edge.
- Vol targeting halves daily std, the 30% steadiness score.
- Engine checks passed: buy-and-hold matches a direct price calculation
  exactly; feeding future weights (1 hour of lookahead) raises Sharpe from
  -0.8 to 25, so the honest version does not leak.

## E3. Trend lookback sweep, IS only (Oct 6, rerun Oct 7)
**Tried:** 8 lookback combinations (1d to 30d) on ALL10, in-sample only.
**Reproduce:** part of `python -m backtest.run_baselines`

**Results:** with v0 costs only the 30-day lookback was positive (+1.2%).
With realistic costs (E4) every lookback loses: best is 30d at -4.0%,
worst 1d at -25%.

**Failure:** no trend setting survives realistic costs. Shorter lookbacks
trade more and lose more. Trend was also broken by thin markets (ALL10 IS
negative vs CLEAN7 IS +7.9% at low costs).

## E4. Baselines with realistic costs (Oct 7)
**Tried:** same strategies; costs split into fee, half-spread (10 bp, from
live book) and funding (constant APR per market, from a 40-minute sample).
**Reproduce:** `python -m backtest.run_baselines` (commit `c140d2b`)

**Results (ALL10, OOS return, adding one cost at a time)**
| Strategy | No costs | + fee | + spread | + funding |
|---|---|---|---|---|
| Buy-and-hold | 20.5% | 20.5% | 20.5% | 18.5% |
| Risk parity 10% vol | 7.6% | 7.4% | 7.2% | 4.4% |
| Trend long/short | 7.1% | 5.0% | 0.8% | 1.2% |

- Every baseline lost money IS once all costs were included.

**Learned**
- Spread is what kills trend (fees + spread ~6% over the period).
- Funding is the largest cost for anything that holds positions.
  Risk parity put ~62% of capital in USDEUR + USDJPY (low vol); at
  9-33%/yr funding, long low-vol markets lose money.
- **Caveat that turned out to matter:** the funding constants came from 40
  minutes of data and were wrong (see E7).

## E5. Leverage vs volatility (Oct 7)
**Tried:** leverage 0.5x-5x (multiplier on a 10% vol target) for the best IS
baseline (trend long/short on crypto) and the most consistent one (risk
parity on CLEAN7). Scored over every 14-day window, under two guesses of
the field's range ("narrow": return -5..+10%, std 0..2%; "wide": -15..+30%,
0..4%). **Reproduce:** `python -m backtest.leverage_study`

**Results:** in every case (both strategies, IS and OOS, both field
guesses) the lowest leverage scored best: 0.5x scored ~40-41/70, 5x scored
19-29/70. Drawdowns at 5x: -21% to -35%.

**Learned**
- Return and daily std scale together with leverage, so the score is a
  straight line in leverage and the optimum is a corner. Leverage only helps
  if a 14-day return is ~5-6x its daily std (roughly Sharpe > 7).
- Recommendation: 0.5-1x on a 10% vol target (0.3-0.6% daily std), hard
  cap 2x. Our edge has to come from a better strategy, not more size.
- Uncomfortable finding: under these assumptions a team that barely trades
  scores ~43, above all our strategies. The minimum-volume rule partly
  prevents that.

## E6. First live par sample, 40 minutes (Oct 6)
**Tried:** spreads and funding from the laptop recording, Oct 6 02:05-02:45
UTC (`data/`).

**Results:** spread a fixed ~20 bp in every market; quotes rarely change
(BTC oracle changed 203 times, best bid 6 times); ~$50k at the best BTC
level; funding +44%/yr HYPE, +11% BTC/ETH, -25% EWJ.

**Failure:** the funding numbers were not representative (see E7). 40
minutes from one evening is too little to set any cost assumption.

## E7. Live market microstructure, 28 hours (Oct 7)
**Tried:** server recording, Oct 6 02:35 to Oct 7 18:33 UTC, 5 s snapshots,
27.8 h after gaps. Measured spread, depth, drift from oracle, catch-up
speed, funding, cost ranking. **Reproduce:** `python -m backtest.live_market`

**Results**
- Spread 19-20 bp everywhere. One bot, one order per level, same ladder in
  every market: ~$50k at 10 bp from mid, then $50-100k at 15-20 bp, $200k at
  35 bp, $300k at 55 bp (~$650k per side crypto/BRENT/EWY, ~$350k
  SP500/GOLD/FX). Whole book is only ~7-8 levels per side.
- Round-trip cost vs oracle incl. 2 x 5 bp fee: ~30 bp to $50k, 40 bp at
  $100k, 58 bp at $250k.
- Mid vs oracle: 3-5 bp SP500/FX, 7-11 bp crypto/GOLD/EWY, 29 bp BRENT
  (an episode Oct 6 18:00 - Oct 7 01:00 UTC at -90 to -155 bp), 44 bp EWJ
  (drifted up to +88 bp; best bid above oracle 75% of the time).
- Oracle changes in 40-86% of 5 s steps; bot quotes in 0.4-6%. After an
  oracle jump the mid covers half the move in a median 15-45 s, and only
  ~70% of jumps within 10 min. Mark tracks the oracle immediately.
- Funding follows mid-minus-oracle drift (hourly correlation 0.80-0.99).
  Averages: EWJ +197%/yr (to +470%), BRENT -123%/yr (to -850%), crypto
  +25-41%/yr with spikes above 400%.
- Cost ranking: round trips are tied (~30 bp at $50k); ranked by drift:
  SP500, USDEUR, USDJPY cheapest; BRENT, EWJ riskiest (buying EWJ cost
  61 bp vs oracle, selling earned 31 bp).

**Learned**
- The constant-funding model in `costs.py` is wrong. Funding must be
  modelled from the book's drift vs oracle.
- Possible edge: when quotes sit away from the oracle, trading toward the
  oracle gains vs our scoring price and earns funding. Ask organizers
  whether this counts as an "attack" before relying on it.
- Caveats: weekday data only, before other teams trade; the bots may
  change at the competition start.

## E8. Sensitivity of analysis settings (Oct 7)
**Tried:** the jump definition (>= 2/3/5 bp, top 10/5/1%), the "caught up"
share (50/75/100%), and the US-hours window (12-21, 13-20, 14-20 UTC).
Settings were fixed Oct 7, before later data. **Reproduce:**
`python -m backtest.live_market` (prints the grids)

**Results**
- Catch-up holds under every setting: median 10-68 s; 8-46% of jumps not
  caught within 10 min. Bigger jumps are caught faster (10-15 s vs 30-60 s).
- "Spreads are tighter during US hours" does not hold: under 1.6 bp under
  every window.

**Learned / dropped:** report catch-up as a range, not one number. Dropped
the US-hours spread claim.

## E9. Recorder upgrade (Oct 7, infrastructure)
**Problem:** 5 s polling was too coarse for catch-up timing, and only 5
book levels were saved. The first recorder also had two gaps (Oct 6
06:48-18:50 UTC, Oct 7 ~18:00-18:13 UTC) before the watchdog was fixed.

**Did:** `record_ws.py` (websocket): oracle/mark/funding ~1 s on change,
best bid/ask on every change with exchange timestamp, full book on change.
Tested 75 s locally (222 ctx, 8 bbo, 25 book rows). Deployed on the VM
alongside the old recorder; watchdog covers both.

**Open:** reconnection after a real network drop not yet observed;
`live_market.py` needs a loader for the new file format.

## E10. Live test trade (Oct 7, in progress)
**Tried:** $1,000 long par:BTC to confirm fee rate, funding payments and
fill quality vs oracle. **Reproduce:** `python test_trade.py plan|open|status|close|report`

**So far:** filled 0.012 BTC at 83,342 (best ask, 9.9 bp below oracle) at
~19:20 UTC. Position confirmed on the exchange. Funding then quoted at
-128%/yr (longs receive). Waiting for 3 hourly funding payments, then close.

**Results:** _pending_

---

## Open questions for organizers
- Does daily equity for the std score use oracle or mark?
- Fees on par; maker rebate?
- Is the balance reset to 1,000,000 USDPR at the start, and do practice
  trades count?
- Is trading against stale bot quotes allowed?
- ~~Is outside data allowed?~~ Yes (confirmed Oct 7).
