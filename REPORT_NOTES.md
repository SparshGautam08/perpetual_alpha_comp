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

**Observed on the VM (Oct 7):** the server closes the websocket with
"code 1000: Expired" every ~11 minutes (19:15, 19:26, 19:37, 19:48 UTC),
even though we ping every 45 s, so it looks like a fixed session lifetime.
The recorder reconnects in 1-2 s each time, losing ~0.2% of the timeline.
Any websocket-based bot feed must expect this.

**Open:** `live_market.py` needs a loader for the new file format; gaps of
1-2 s every ~11 min should be marked like the polling gaps.

## E10. Live test trade (Oct 7)
**Tried:** $1,000 long par:BTC to confirm fee rate, funding payments and
fill quality vs oracle. **Reproduce:** `python test_trade.py plan|open|status|close|report`

**So far:** filled 0.012 BTC at 83,342 (best ask, 9.9 bp below oracle) at
~19:20 UTC. Position confirmed on the exchange. Funding then quoted at
-128%/yr (longs receive). Waiting for 3 hourly funding payments, then close.

**Results so far**
- **Taker fee is 9.0 bp** ($0.90 on $1,000), not the 5 bp assumed in E2-E7.
  `costs.py` updated to 9.0. Round trip at $50k is ~38 bp vs oracle, not
  30. Backtest results in E2-E5 are therefore slightly optimistic; rerun
  pending. Maker fee still unknown.
- In unified mode the USDPR "total" already includes unrealized P&L at
  mark (1,000,000 - 0.90 fee - 0.64 uPnL = 999,998.46). Scored equity at
  oracle = total - uPnL@mark + uPnL@oracle (`balance.py`).
- Closed Oct 7 ~22:03 UTC: sold 0.012 at 83,254. Net -$2.77 = price
  -$1.06, fees -$1.80 (9.0 bp on both sides), funding +$0.09.
  Balance 999,997.23 (pre-competition; balances reset at the start).
- **Funding is paid hourly as position x oracle x rate**, sign as
  expected: 20:00 UTC rate -0.0101%/h, we (long) received $0.101; 21:00
  -0.0020%/h, +$0.020; 22:00 +0.0034%/h, paid $0.034.
- **Both fills beat the oracle:** bought 14.4 bp below it, sold 7.7 bp
  above it, because the bot's quotes lagged the oracle (E7). Two fills
  prove nothing, but it is a live example of the drift effect.
- Order flow worked end to end (IoC fill, position, close); no surprises.

## E11. Trading bot v1, paper mode (Oct 7)
**Built:** `bot/` package. Strategy = risk parity on CLEAN7 at 0.5 x 10% vol
target (E4-E5), weights from the backtester's own `risk_parity()` on 40 days
of mainnet hourly candles (outside data). Daily rebalance at 00 UTC,
1% no-trade band, IoC orders capped at 15 bp from mid, $25k slices, caps
of 30% per market and 1.5x gross, daily loss stop -2%, STOP/PAUSE files,
ntfy phone alerts. Paper broker fills against the live par book (all
levels within the limit), charges 9 bp and hourly funding.
**Reproduce:** `python -m bot.main --once --rebalance-now` (paper)

**Results (first paper rebalance, Oct 7 ~19:48 UTC)**
- Targets: SP500 12.9%, GOLD 6.1%, EWY 4.1%, BTC 3.9%, ETH 3.2%, BRENT
  2.9%, HYPE 2.0% (gross ~0.35).
- Entering ~$350k cost $738 at oracle (0.07% of equity): $315 fees plus
  fills ~10 bp above oracle (GOLD ~20 bp, BTC ~0.4 bp).
- Kill switch flattened all 7 positions; a simulated -4.8% day triggered
  the loss stop, flattened, and blocked trading for the rest of the UTC day.

**Bugs found in testing (fixed)**
- A $32 order sat in front of the bot's $50k BRENT level; paper fills only
  used the first level, so the bot sent 139 tiny orders. Fixed: paper fills
  walk all levels within the limit; execution stops a market when a slice
  fills < 50%; max 20 slices per market.
- Price limit was relative to the best quote (the tiny order), so the real
  level was out of reach. Fixed: limit relative to mid (15 bp).
- Summary reported "unfilled: none" while BRENT was $28.7k short; now
  reports shortfall per market.

**Open:** live mode not yet run; reconnection/long-run stability untested;
strategy is a baseline (no funding awareness, no drift signal yet).

## E12. Mainnet price recording (Oct 7, infrastructure)
**Why:** par's oracle is a median across venues including Hyperliquid
mainnet, and par's bot quotes lag the oracle by 10-60 s (E7-E8). To test
whether mainnet moves first, we need mainnet and par recorded at the same
moments; the historical candles are hourly and can't show this.
**Did:** `record_ws.py --venue mainnet`: per market, mainnet oracle/mark/
mid/funding (~1 s) and best bid/ask price changes with exchange time. Size
-only book updates skipped (831 -> 87 rows per 45 s); ~55 MB/day.
Mainnet's full book not recorded (updates every block).
**Open:** lead-lag analysis once a few days are recorded.

## E13. Funding + drift strategy, development data (Oct 8)
**Tried:** when par's mid is >= E bp from the oracle, trade toward the
oracle (sell when mid is above it, buy when below); exit when the gap is
back within X bp or after H hours. $25k per trade, fills walk the recorded
book, 9 bp fee each side, hourly funding at recorded rates, forced exit at
recorder gaps. Grid: E in {15,20,30,40,60}, X in {0,5,10}, H in {1,4,12},
with or without "funding must pay our side". Development data only (Oct 6
02:35 to Oct 7 18:33 UTC). **Reproduce:** `python -m backtest.drift_strategy`

**Results**
- Below 30 bp it loses heavily (fees + spread): median -$14k at 15 bp,
  -$5k at 20 bp. Around 30 bp it is near zero; 40-60 bp is positive.
- Best setting (E=40, X=0, H=12): 18 trades in 7 markets, net +$826:
  entry edge +$1,320, exit cost -$782, funding +$324, fees -$810,
  **oracle move +$774**. Net excluding oracle moves: **+$52**.
- E=60: 7 trades, net +$572; one BRENT trade (+$590) is more than all of it.
- "Funding must pay our side" barely changes anything: funding almost
  always agrees with the gap's sign (consistent with E7).
- Flipped strategy (opposite side of every signal): -$4,474, 0% wins.
  Weak evidence by itself, since flipping also pays costs twice.

**Learned**
- Break-even math matches the data: entry gains (gap - ~10 bp half-spread),
  exit costs ~10 bp, fees 18 bp, so the gap must be ~38 bp+ to break even
  before funding. The grid turns positive right there.
- The profit only appears if the gap actually closes. EWJ's gap never
  closed: exits gave back the entry edge (-$667), funding only partly
  compensated (+$150).
- Forced exits at recorder gaps cost ~-$460 (4 trades); this is a
  simulation artifact, not something the live bot would do.
- Not enough evidence: 28 hours, ~2 dominant episodes (BRENT Oct 6 evening,
  EWJ Oct 7). Treat as a hypothesis.
- Ideas not tested yet: exiting with resting (maker) orders instead of
  crossing the spread; sizing by gap size; holding through to collect
  funding when the gap persists.

**LOCKED for testing (Oct 8, before any test data was seen)**
- Rule: enter when |gap| >= 40 bp and funding pays our side; exit when the
  gap is back to 0 bp or after 12 h; $25k per trade; all 10 markets.
- Test data: everything after 2026-10-07 18:33:23 UTC.
- Success criterion: net P&L excluding oracle moves (edge + funding + fees)
  > 0 on the test data. Also reported: net incl. oracle moves, trade count,
  per-market breakdown.

## E14. Drift strategy in the bot; official rules found (Oct 8)
**Built:** `bot/strategy.py: Drift` implements the E13 locked rules exactly
(40 bp entry, exit at 0, 12 h max, funding must pay our side, $25k, all 10
markets), checked every loop (60 s). Run with `--strategy drift`; own state
and paper files, separate from risk parity. Entry/exit code tested in paper
mode with a temporary 1 bp threshold.

**Bug found and fixed:** closing converted the position to $ at the oracle
and back to units at the fill price, leaving dust (e.g. EWY -0.112). Closes
(and the kill switch) now trade the exact position size in units.

**Observation:** at 20:40 UTC Oct 8 every par market was within 3 bp of
the oracle and funding was at its baseline (5-11%/yr). The large EWJ/BRENT
gaps of Oct 6-7 were not present. The drift rule would not trade at all in
such conditions; whether gaps come back is exactly what the test measures.

**Official rules (perpetual-alpha26.github.io):** scored phase Oct 19 -
Nov 6; taker 9 bp (matches E10), maker 3 bp; prohibited list does not
include trading against posted market-maker quotes. Minimum notional volume
not published. Implication: exiting with resting maker orders (3 bp, no
spread crossing) instead of taker orders (9 bp + ~10 bp) could cut the
drift strategy's round-trip cost from ~38 bp to ~22 bp. Untested; would be
a new experiment, not a change to the locked rule.

## E15. Locked drift rule, out-of-sample test: FAILED (Oct 9)
**Test:** the E13 rule exactly as locked, on data after 2026-10-07 18:33
UTC (to Oct 9 ~03:00 UTC, ~32 h, 233,560 snapshots). Only the 5 s
recorder's files were used. **Reproduce:** `python -m backtest.drift_strategy --test`

**Results**
| | Dev (Oct 6-7) | Test (Oct 7-9) |
|---|---|---|
| Trades | 18 | 16 |
| Net incl. oracle moves | +$826 | +$455 |
| Fill edge (entry + exit) | +$538 | +$13 |
| Funding | +$324 | +$263 |
| Fees | -$810 | -$719 |
| **Net excl. oracle moves (criterion)** | +$52 | **-$443 -> FAILED** |
- Profit including oracle moves (+$898 of oracle moves) is luck, not edge.
- Flipped check: -$3,625.
- Gap frequency changed: share of time |gap| >= 40 bp fell for BRENT
  (23% -> 2.4%), ETH, EWY; EWJ still ~47%; HYPE rose (1.3% -> 5.2%).

**Diagnosis:** other traders are now on par. The top of the book now
often holds small orders ($150-$1,900) with 0-6 bp spreads, not just the
organizer bot's ~$50k levels 20 bp wide. The rule measures the gap from
the top-of-book mid, which these small orders distort. Example, HYPE Oct 8
16:03-18:12 UTC: 7 entries in a row with negative entry edge; at those
moments a $25k buy filled 2-8 bp ABOVE the oracle although the mid looked
far below it.

**Learned**
- A signal must be measured at the price we can actually trade at for our
  size, not at the top-of-book mid.
- The market is changing as other teams arrive; any cost or gap statistic
  from Oct 6-7 is out of date. This is why the bot must measure live.
- Next (new experiment, new lock): enter only when the $25k executable
  price is >= E bp better than the oracle; test on data after Oct 9.

**Live bot (same v1 rule, VM, since Oct 8 21:01 UTC):** one trade so far,
BTC Oct 9 02:07-02:08 UTC: bought ~0.247 BTC ~81,700 when par was > 40 bp
below the oracle, sold one minute later ~82,040. Price +$82, fees -$36,
net ~+$46. Account 1,000,042.98.

## E16. Drift v2: signal at the executable price (Oct 9)
**Change from v1:** enter when a $25k fill (walking the book) is >= E bp
better than the oracle (sell above it / buy below it); exit when closing
would cost <= C bp vs the oracle, or after H hours. v1 used the top-of-book
mid, which small orders from other traders distort (E15).
**Design data:** all data seen so far (Oct 6 02:35 - Oct 9 03:05 UTC),
since the v1 diagnosis already looked at it. Also reported separately for
the early (Oct 6-7) and late (Oct 7-9) halves.
**Reproduce:** `python -m backtest.drift_strategy --v2`

**Results (net excluding oracle moves; grid E in {15,20,30,40}, C in
{5,10,15}, H in {4,12}, funding required or not)**
| E / C / H | Trades | Total | Early half | Late half |
|---|---|---|---|---|
| 40 / 5 / 12 | 16 | +$1,105 | +$611 | +$421 |
| 40 / 10 / 12 | 16 | +$972 | +$513 | +$385 |
| 30 / 10 / 12 | 24 | +$874 | +$344 | +$457 |
- Median by entry edge: 15 bp -$1,403; 20 bp -$287; 30 bp +$622; 40 bp +$766.
- 30-40 bp is positive in BOTH halves, including the period where v1
  failed. Requiring funding to pay our side doesn't help; dropped.
- 12 h holds beat 4 h (more time for the gap to close cheaply).
- Book now much tighter: on Oct 9 03:09 UTC a $25k trade filled 1-11 bp
  from the oracle in every market (vs ~20 bp on Oct 6-7).

**LOCKED v2 (Oct 9, before any data after 03:05 UTC was seen)**
- Rule: entry when the $25k fill is >= 40 bp better than the oracle; exit
  when closing costs <= 10 bp vs the oracle, or after 12 h; $25k; all 10
  markets; no funding condition. (Middle of the positive region, not the
  best cell.)
- Test data: everything after 2026-10-09 03:05 UTC.
- Success criterion: net excluding oracle moves > 0 on the test data.
- Bot: `bot/strategy.py: Drift` (name drift_v2) implements exactly this,
  measuring fills from the live book every 60 s.

## E17. Combined bot: risk parity base + drift v2 (Oct 9)
**Why:** drift alone may not trade for days, but the rules require trading
on >= 10 of ~19 scored days plus a minimum volume. Risk parity rebalances
daily (activity, volume, steadiness); drift adds opportunistic trades.
Running the base live also tests the backtester's cost/fill assumptions
at real size (~$340k vs drift's $25k).
**Design ("sleeves", `bot/portfolio.py`):** drift's share of each market is
tracked exactly in state (what its trades opened/closed); risk parity's
share is the rest (its daily targets, in units, held between rebalances).
Each loop the bot trades only the difference between the summed wishes and
the actual position, so opposite wishes net out (fewer fees). Drift's exits
only see its own share. Kill switch / loss stop flatten and clear targets.
Run with `--strategy combined`; `touch bot/state/REBALANCE` forces a
rebalance on the next loop.
**Paper test (Oct 9 03:17 UTC):** RP built 7 positions (BTC $36.6k ...
SP500 $130k; cost $405 at oracle). Forced drift short BTC against RP's
long: one net sell, actual 0.1415 = RP 0.4460 + drift -0.3045. Forced
drift exit: BTC back to 0.4462 (target 0.4460), drift share empty. Kill
switch: all flat, targets cleared.
**Open:** drift's P&L can't be read from the account alone; log sleeve
units each heartbeat and attribute P&L offline.

## E18. Live incident: drift exits crashed for ~9 hours (Oct 9)
**What happened:** drift v2's first live trades: sold ~$25k ETH (Oct 9
~10:30 UTC) and ~$25k BTC (~12:50 UTC), both par prices >= 40 bp above
the oracle, netted against risk parity's longs. From ~14:00 UTC every loop
failed when trying to close them; ntfy "errors" alerts every 10 min. The
positions stayed open past the 12 h limit and the loss stop could not act.
**Cause:** after an entry, drift's share was stored as a numpy float in
memory; the exit order's is_buy / reduce_only became numpy booleans; the
SDK's msgpack signing raises "can not serialize 'numpy.bool'". Entries
worked (previous share was 0, a plain float). The paper test missed it
because the paper broker doesn't sign orders.
**Fix:** plain Python types throughout the order path, plus a final cast in
LiveBroker.order. Regression test signs an exit built from numpy inputs.
**Lessons:** (1) paper tests must exercise the real signing path;
(2) a repeated error should escalate (e.g. auto-pause + urgent alert)
rather than repeat quietly; (3) with errors since 9 AM CT, nobody acted for
9 hours: alerts need a clear "what to do" line.
**Account at discovery (22:54 UTC):** equity at oracle 1,000,586 (+$642
since Oct 9 04:00 UTC), funding -$73.50 over 133 payments.

## E19. Drift v2 locked test: criterion PASSED, but adverse selection (Oct 9)
**Test:** locked v2 rule (E16) on data after 2026-10-09 03:05 UTC, to
23:09 UTC (~20 h, 144,020 snapshots). **Reproduce:**
`python -m backtest.drift_strategy --v2 --test`

**Results:** 9 trades (all sells: ETH x5, BTC x2, BRENT, HYPE).
| Entry edge | Exit cost | Funding | Fees | Excl. oracle | Oracle moves | Net |
|---|---|---|---|---|---|---|
| +$1,107 | -$517 | +$93 | -$405 | **+$278 PASSED** | -$427 | -$149 |
- Entries really were 100-130 bp above the oracle (the v1 measurement
  problem is fixed).
- Backtest matches live: the live bot entered ETH at 10:3x and BTC at
  12:5x UTC, the same moments as the backtest's first two trades.
- **Oracle moved against us in 7 of 9 trades:** when par's bids sit far
  above the oracle, the oracle tends to rise to meet them. Likely informed
  traders (e.g. watching mainnet) buying on par before the oracle updates;
  selling to them is adverse selection.
- Flipped check not meaningful for v2 (re-enters almost constantly, 664
  trades).
- Two ETH trades ended at short recorder gaps (simulation artifact).

**Learned:** the strict criterion passed, but n = 9 and the all-in result
was slightly negative. The adverse-selection pattern points to the
mainnet lead signal as a filter (skip gaps that mainnet says are
"informed") and possibly as its own strategy. Next: E20.

## E20. Mainnet leads par's oracle by ~5-9 seconds (Oct 9)
**Data:** websocket recordings of par (ctxws) and Hyperliquid mainnet (bbo
mid, ctxws) on the same VM clock, 1 s grid, Oct 7 22:39 - Oct 9 23:09 UTC
(174,571 s). **Reproduce:** `python -m backtest.lead_lag`

**Results**
- Lead/lag (correlation of 1 s returns, mainnet shifted -30..+30 s): par's
  oracle best matches mainnet 5 s later (BTC, ETH, HYPE), 7-9 s later
  (SP500, EWY, EWJ, BRENT, GOLD, FX); corr 0.17-0.27 at the best lag vs
  ~0.00-0.04 at lag 0. Par's book mid also lags mainnet by 4-8 s.
- Prediction: premium = mainnet mid / par oracle - 1. Correlation with the
  par oracle's next-5 s change: 0.77 BTC, 0.78 ETH, 0.66 BRENT, 0.64 HYPE,
  0.55-0.57 SP500/EWY. When |premium| >= 3 bp, the oracle moves that way
  within 5 s: BTC 98.6%, ETH 97.7%, SP500 100% (n=359), BRENT 93.1%,
  USDEUR 97.3% (n=520), EWY 81.5%; HYPE/EWJ/GOLD/USDJPY 52-62%.
- HYPE, EWJ, GOLD, USDJPY have a persistent mainnet-vs-oracle offset (median
  |premium| 4-5 bp) that hides the signal; measure against a rolling
  baseline next time.
- Drift link (E19 question): premium at the 9 drift v2 entries was -11 to
  +3 bp and did not predict the oracle's next-30-min move. The lead works
  over seconds; drift trades last hours. Hypothesis not supported.

**Learned:** mainnet is a strong, fast predictor of par's oracle, and
par's book lags too, so one can buy on par at a stale price before the
oracle (our scoring price) catches up, without needing to sell quickly.
Costs (9 bp fee + spread) mean only large mainnet jumps can pay; frequency
and P&L to be measured in E21.

## E21. Lead signal as a standalone taker strategy: does NOT pay (Oct 9)
**Tried:** premium = mainnet mid vs par oracle minus its 30-min rolling
median; |premium| >= K bp -> buy/sell $N on par, close after H s. Fills
from the recorded par book (data_ws/l2): optimistic = last book seen;
conservative = next book update. 9 bp fee each side. Grid K in
{10,15,20,30}, H in {30,60,300} s, N in {$10k,$25k}. Design data Oct 7
22:39 - Oct 9 23:09 UTC. **Reproduce:** `python -m backtest.lead_strategy`

**Results:** negative in every cell except K=30 (8-10 trades, about
breakeven: -$23 conservative / +$33 optimistic at $10k, H=300).
Per trade, optimistic fills, $10k:
| Signal | Trades | Oracle move | Entry vs oracle | Exit vs oracle | Fees | Net |
|---|---|---|---|---|---|---|
| ~12 bp | 582 | +11.2 | -6.3 | -9.9 | -18.0 | -23.0 |
| ~18 bp | 149 | +16.3 | -8.1 | -11.8 | -18.0 | -21.6 |
| ~22 bp | 53 | +24.1 | -5.7 | -14.1 | -18.0 | -13.7 |
| ~33 bp | 8 | +35.0 | -33.5 | +20.6 | -18.0 | +4.1 |
(bp of notional)

**Learned**
- The forecast is excellent: the oracle moves by ~the full signal.
- A taker round trip costs ~35 bp (18 bp fees + entry 6-8 + exit 10-14);
  par's book is NOT as stale as hoped (other traders update it), so we
  can't buy at the old price cheaply.
- Only signals > ~35 bp pay; rare (8 in two days). Rejected as a
  standalone strategy.
- Ways to use the forecast without paying for it: (1) time trades the bot
  must make anyway (rebalances, drift exits, volume) to when mainnet
  predicts a favourable oracle move: ~10-20 bp gain, zero extra fees;
  (2) maker quotes around a mainnet-based fair price (3 bp fee, earn the
  spread), with adverse-selection risk from other informed traders.

## E22. Par trades feed added to the recorder (Oct 9, infrastructure)
**Why:** a maker strategy needs to know whether a resting quote at price X
would have been hit, which only the stream of actual trades shows; it also
measures informed flow and total volume.
**Did:** `record_ws.py` (par mode) subscribes to `trades`: price, size,
aggressor side, exchange time, trade id, buyer/seller addresses (public
on-chain). The feed replays the last 30 trades on every connect, so rows
are de-duplicated by trade id. Deployed on the recorder VM Oct 9 23:20 UTC.
**First observation:** par is very quiet. In a 70 s test no new trade
occurred; the latest BTC trade was ~5 h old. One address
(0x1ba2...1ca4) was on one side of every sampled trade, likely the
organizers' market maker. Maker fills from other traders may be rare
until the scored phase.

## E23. ML Model 1: oracle forecast, walk-forward (Oct 9)
**Task:** predict par's oracle change over the next H s (bp), H = 5/15/30,
from 20 features available at time t: mainnet-vs-oracle premium (raw,
offset-removed, and 1-10 s lags), mainnet returns (1-60 s) and 5-min vol,
par oracle returns (5-60 s) and seconds since its last change, par book
mid vs oracle, mainnet's own oracle vs par oracle, hour, US session flag,
market id. Pooled over 10 markets, 1.74M one-second rows (Oct 7 22:44 -
Oct 9 23:08 UTC).
**Models:** baseline = OLS on the offset-removed premium (the E20 rule);
ridge on all features; HistGradientBoosting (300 iters, lr 0.05, 31
leaves, min leaf 200, l2 1.0, market as categorical).
**Validation:** walk-forward, expanding train window (first 24 h), 4 test
blocks of 6 h, 5-min purge gap. All metrics out of sample.
**Reproduce:** `python -m backtest.ml_forecast`

**Results (mean over folds)**
| H | Model | R2 | Corr | Hit, top 10% | Move, top 10% | Move, top 1% |
|---|---|---|---|---|---|---|
| 5 s | baseline | 0.35 | 0.63 | 92.2% | 1.6 bp | 3.8 bp |
| 5 s | GBM | 0.64 | 0.80 | 97.6% | 2.1 bp | 4.7 bp |
| 15 s | baseline | 0.23 | 0.50 | 86.8% | 2.4 bp | 5.3 bp |
| 15 s | ridge | 0.35 | 0.59 | 90.9% | 2.8 bp | 6.0 bp |
| 15 s | GBM | 0.36 | 0.60 | 91.1% | 2.8 bp | 6.0 bp |
| 30 s | baseline | 0.12 | 0.36 | 78.0% | 2.4 bp | 5.5 bp |
| 30 s | GBM | 0.17 | 0.42 | 81.0% | 2.8 bp | 5.9 bp |
- GBM > ridge > baseline in every one of the 4 folds at 15 s (GBM R2
  0.34-0.37 vs baseline 0.20-0.30).
- Per market (last fold, corr): biggest gains where the raw premium was
  weak: GOLD 0.19 -> 0.53, EWJ 0.27 -> 0.54, USDJPY 0.23 -> 0.42, SP500
  0.44 -> 0.68.
- Ablation: ridge is nearly as good as GBM at 15-30 s, so most of the gain
  comes from the features, not non-linearity. GBM's extra edge is at 5 s.

**Learned:** a much better short-term fair price than the raw premium.
Predicted moves are small (top 10% ~2.8 bp, top 1% ~6 bp), far below
taker round-trip costs (~35 bp, E21), so the model's use is as the fair
price for maker quotes and for timing necessary trades, not as a taker
signal.

**LOCKED (Oct 9, before any data after 23:09 UTC was seen):** features,
GBM hyperparameters, training scheme and H = 15 s as above. Test: train on
all design data, evaluate on data after 2026-10-09 23:09 UTC. Success
criterion: GBM out-of-sample R2 at 15 s exceeds the baseline's.

## E24. Live drift v2 trades, Saturday Oct 10 (live log)
Three HYPE round trips by the combined bot (fills from the exchange):
| Time (CT) | Trade | Held | Price P&L | Fees | Net |
|---|---|---|---|---|---|
| 09:13 | buy below oracle, sell | 3.3 min | +$293 | -$45 | +$248 |
| 09:25 | buy below oracle, sell | 1.1 min | +$129 | -$45 | +$84 |
| 11:24 | sell above oracle, buy back | 1.1 min | +$170 | -$45 | +$125 |
Total ~+$457 net; 3/3 profitable, all closed within minutes as the gap
closed (crypto trades through the weekend). Equity at oracle 1,001,649 at
17:11 UTC (+$1,063 since Fri 23:53 UTC); funding -$45 since then.
**Bug found:** partial exits (drift closing on top of risk parity's
position) were sized in $ at the oracle, so each sold ~0.5% less than the
entry bought; 3.3 HYPE (~$280) was left over. Fixed: size in exact units
(`close_units`); paper round trip now returns to the RP target exactly.

## E25. More drift trades: check frequency, and a 30 bp variant (Oct 10)
**Check frequency** (locked v2 rule, snapshots thinned to every 5/15/30/60 s):
| Check every | Trades Oct 6-9 | Net | Excl. oracle | Trades test period (Oct 9) |
|---|---|---|---|---|
| 5 s | 25 | +$1,777 | +$1,250 | 9 |
| 15 s | 23 | +$1,494 | +$1,212 | 8 |
| 60 s (live bot) | 20 | +$1,520 | +$1,278 | 8 |
Checking faster adds only ~20% trades; the limit is that >= 40 bp
executable gaps are rare (~7/day across 10 markets).

**LOCKED variant v2b (Oct 10, before data after 2026-10-09 23:09 UTC was
seen):** identical to v2 but entry edge >= 30 bp (exit <= 10 bp, 12 h,
$25k, all 10 markets). In the v2 design grid (E16) 30 bp gave ~50% more
trades and was positive in both halves. Test on data after 2026-10-09
23:09 UTC, alongside v2 on the same data. Success criterion: v2b net
excluding oracle moves > 0 AND v2b total net >= v2 total net on that data.

---

## Correction to E15/E19 (Oct 10)
E15 attributed the Oct 8 change in par's book (small orders, 0-6 bp
spreads instead of the fixed ~20 bp ladder) to "other teams arriving". The
organizers had said on Oct 7 that Keyrock's market-making bots would start
during practice "hopefully by tomorrow" (Discord #ask-anything). The change
is more likely Keyrock joining or replacing the initial organizer bot. This
also fits the adverse selection in E19: a professional market maker quotes
from fast external prices, so its quotes away from the oracle are often
informed. Our trade data (E22) can confirm who quotes by address.

## Open questions for organizers
(Oct 9-10 answers from Discord #ask-anything: eligibility = >= 1 trade, no
volume or day minimum; new DEX for the live phase; deadline Nov 6; bot
updates allowed; funding parameters = mainnet. See CLAUDE.md.)
- Does daily equity for the std score use oracle or mark?
- ~~Fees on par?~~ Taker 9 bp, maker 3 bp (official docs).
- ~~Is the balance reset at the start?~~ Yes (confirmed Oct 7).
- ~~Do practice trades count toward the 10 trading days / minimum volume?~~
  No, those rules apply only to the live competition (confirmed Oct 7).
- Is trading against stale bot quotes allowed?
- ~~Is outside data allowed?~~ Yes (confirmed Oct 7).
