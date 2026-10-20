# Perpetual Alpha 2026: trading bot

Our entry to [Perpetual Alpha](https://perpetual-alpha26.github.io), the
algorithmic trading competition on a HIP-3 perpetuals exchange on the
Hyperliquid testnet (ICAIF 2026). The scored phase runs Oct 19 12:00 UTC to
Nov 6 12:00 UTC on the `pas` exchange (practice ran on `par`), with ten
markets: BTC, ETH, HYPE, SP500, EWY, EWJ, BRENT, GOLD, USDEUR and USDJPY.

The full research log, with every experiment, result, failure and how to
reproduce it, is in [REPORT_NOTES.md](REPORT_NOTES.md) (E1 to E42). This
README is the map.

## What the bot does

**Drift (the live strategy).** The competition's order books sometimes sit
away from the oracle price that positions are scored at. When a $25k order
would fill at least 30 bp better than the oracle, the bot takes that side
and exits once closing costs at most 10 bp (or after 12 h). It checks every
15 s and holds at most 4 positions at once. Designed in E13/E16, passed
locked out-of-sample tests in E19 and E27, reconciled with live trading in
E30.

**ML Model 1 (oracle forecast).** A gradient-boosted model predicts the
oracle's move over the next 15 s from mainnet Hyperliquid prices, the
mainnet-vs-oracle premium, oracle staleness and time features (E23).
Out-of-sample R2 0.32 vs 0.16 for a simple rule (E27); 0.36 vs 0.31 live in
shadow mode, better in all 10 markets (E35). In the live bot it filters
drift's entries: a gap the oracle is predicted to close by itself is
skipped (E39).

**Funding carry (from Oct 21).** On `pas`, funding has been one-sided
for days (longs paid ~100%/yr on SP500). The bot holds $50k of SP500 on
the side that receives funding while the last 24 h of funding pays at
least 30%/yr, and exits below 15%/yr (E46; price risk studied on real
history with the price drift removed).

**Risk controls.** 200 bp catastrophe stop per trade, 2% daily loss stop,
at most 4 open drift positions, a kill switch, and a start gate that keeps
the bot idle until the scored phase starts and the account is funded.

**What we tried and dropped** (details in REPORT_NOTES): trend following
(costs, E3-E4), risk parity (adds daily volatility without return, E38), a
mainnet lead signal as a taker strategy (E21), and an ML market maker
(break-even after fees, E36).

## Repository layout

| Path | Contents |
|---|---|
| `bot/` | The trading bot. `main.py` (loop, kill switch, loss stop, start gate), `strategy.py` (drift, risk parity), `portfolio.py` (combines strategies), `execution.py` (orders), `broker.py` (paper / live), `config.py` (all settings, overridable by environment variables) |
| `bot/features.py`, `bot/livefeed.py`, `bot/shadow.py`, `bot/mlsignal.py` | ML Model 1's inputs (shared by training and live), the live data feed, shadow mode (logs predictions), and the bot's reader of those predictions |
| `bot/maker.py` | ML market maker (paper tests only, E34/E36) |
| `bot/eligibility.py` | Makes one tiny trade if the bot has not traded by a deadline (>= 1 fill is needed to be ranked) |
| `backtest/` | Backtester, baselines and one script per experiment (see below) |
| `models/` | Trained ML Model 1 (`oracle_gbm_v1.joblib`), the baseline, and training metadata |
| `record_ws.py`, `record_market_data.py` | Market data recorders (websocket; 5 s polling) |
| `check_dex.py`, `balance.py` | Read-only checks: a dex's markets and settings; our balance and positions |
| `1_create_team_wallet.py`, `2_setup_account.py`, `3_check_account.py` | Account setup |

## Setup

Python 3.12.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Private keys are never in this repository (`*.key` is git-ignored). The
bot reads its API-wallet key from the file named in `HL_API_KEY_FILE`.

## Running

Paper mode (default, no key, no real orders):

```bash
.venv/bin/python -m bot.main --strategy combined --once
```

Live mode needs `BOT_LIVE=yes`, `--mode live` and `HL_API_KEY_FILE`. The
settings used in the scored phase:

```bash
export HL_DEX=pas HL_COLLATERAL=USDP LEVERAGE=0 DRIFT_ENTRY_BP=30 LOOP_SECONDS=15
export DRIFT_ML_FILTER=yes TRADE_FROM=2026-10-19T12:00:00Z ELIG_TRADE_BY=2026-10-20T20:00:00Z CARRY_USD=50000
BOT_LIVE=yes HL_API_KEY_FILE=~/keys/<api wallet>.key .venv/bin/python -m bot.main --mode live --strategy combined
```

The ML filter needs shadow mode running on the same machine:
`HL_DEX=pas .venv/bin/python -m bot.shadow --out ~/shadow`.

Kill switch: `touch bot/state/STOP` closes every position and stops the
bot. `touch bot/state/PAUSE` stops new orders.

## Data

Not in the repository (size). Sources:

- **Organizers' historical dataset:** hourly mainnet candles, Mar-Oct 2026, in `historical/`.
- **Our recordings of the competition exchange** (`record_ws.py`, from Oct 5): oracle, mark, funding, order books and trades every second, in `server_data/`.
- **Outside data (allowed by the organizers, Oct 7):** public Hyperliquid
  mainnet market data for the markets the competition copies (BTC, ETH,
  HYPE and the `xyz:` SP500, EWY, EWJ, BRENTOIL, GOLD, EUR and JPY perps).
  We use it in three places: as live model inputs (best bid/ask and oracle,
  via websocket, `bot/livefeed.py`), recorded for training
  (`record_ws.py --venue mainnet`), and as hourly price history for risk
  parity's volatility estimates (`bot/market.py`). No other outside data is
  used.

## Reproducing the results

Each experiment in REPORT_NOTES.md lists its command. The main ones:

| Result | Command |
|---|---|
| Baselines with costs (E2-E4) | `python -m backtest.run_baselines` |
| Leverage vs volatility (E5) | `python -m backtest.leverage_study` |
| Drift strategy design and tests (E13-E19) | `python -m backtest.drift_strategy` (`--v2` for v2, `--v2 --test` for its locked test) |
| Backtest vs live (E30) | `python -m backtest.live_vs_backtest` |
| ML Model 1, walk-forward (E23) | `python -m backtest.ml_forecast` |
| Train the deployed model | `python -m backtest.train_oracle_model` |
| Shadow mode scoring (E35) | `python -m backtest.eval_shadow <shadow folder>` |
| Maker A/B (E36) | `python -m backtest.eval_maker <folder>` |
| Risk parity's role (E38) | `python -m backtest.rp_role` |
| ML filter for drift, locked test (E39) | `python -m backtest.ml_drift_filter` |
| Drift size on $100k (E41, E43) | `python -m backtest.drift_size` |
| Drift on pas (E45) | `python -m backtest.pas_drift <pas recordings> <shadow logs>` |
| Funding carry (E46) | `python -m backtest.carry_study` |

Run from the repository root with the virtual environment's Python. The
live-phase evaluation is pre-registered in REPORT_NOTES E42.
