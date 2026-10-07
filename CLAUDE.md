# Hyperliquid trading competition: project context

## Competition
- Trading-bot competition on a HIP-3 perps exchange on Hyperliquid testnet.
- Exchange (dex) name: `par`. Collateral: USDPR (token 3032).
- 10 markets: par:BTC, par:ETH, par:HYPE, par:SP500, par:EWY, par:EWJ,
  par:BRENT, par:GOLD, par:USDEUR, par:USDJPY.
- Scoring (each min-max normalized across teams):
  1. Total net return, marked to oracle at end of live phase.
  2. Low std dev of daily equity returns (steadier = better).
  3. Jury score on a 2-4 page technical report (AI/ML depth, originality,
     rigor/reproducibility, clarity).
- Score weights: profit 40%, low daily-return std dev 30%, report 30%.
- Rules: trade on >= 10 distinct days, hit a minimum notional volume,
  entries must be algorithmic. No wash trading, self-crossing, collusion,
  or attacks on exchange/oracle. Organizer market-making bots quote in all
  markets. Per-market position limits. Fixed bankroll, no top-ups.
- Outside data IS allowed (confirmed by organizers Oct 7), e.g. CME
  futures, prediction markets, mainnet Hyperliquid data.
- Unknown: competition length, min volume, position
  limits, fees, whether practice trades count.

## Account (all set up)
- Endpoint: https://api.hyperliquid-testnet.xyz
- Team wallet (public): 0xC9f1C5a784d2cd5858396299893e71cfe23F4aAf
- Balance: 1,000,000 USDPR. Unified account mode is ON.
- API wallet `bot1` approved: 0x20Cb52dfA3C9934f3ef802FF2D988640532E239C,
  expires ~Nov 5, 2026.
- Bot key file: ~/keys/api_wallet_bot1.key (outside this folder).
  Use via: export HL_API_KEY_FILE=~/keys/api_wallet_bot1.key
- Max 3 API wallets per account; one key per running process.
- Always query with the TEAM address, never the API wallet address.

## Security rules (important)
- NEVER read, print, copy or commit any *.key file, or anything in ~/keys.
- The team wallet key is stored elsewhere; never ask for it.
- .gitignore excludes *.key, .venv/, data/, historical/, server_data/.

## Files in this folder
- 1_create_team_wallet.py: already used, don't rerun.
- 2_setup_account.py: approves API wallets (needs team key, hidden input).
  For a new key: python 2_setup_account.py --name bot2 --days 30 --skip-unified
- 3_check_account.py: read-only balance / mode / agents check.
- bot_template.py: connects with Exchange(api_wallet, URL,
  account_address=TEAM, perp_dexs=["par"]). Coin names need the "par:" prefix.
  Queries: info.user_state(TEAM, dex="par"), info.all_mids(dex="par").
- record_market_data.py: records every 5s to data/ctx_<UTC date>.csv
  (oracle, mark, mid, funding, OI, premium, volume, impact px) and
  data/book_<UTC date>.csv (top 5 book levels per side).
- data/: first ~1 hour of recorded data from the laptop (Oct 5 evening).
- historical/: organizers' historical/backtesting dataset.
- SDK: hyperliquid-python-sdk==0.24.0, Python venv in .venv.
  The SDK's meta_and_asset_ctxs() has no dex arg; post
  {"type":"metaAndAssetCtxs","dex":"par"} directly.

## Live data recorder (Google Cloud)
- VM `recorder` (e2-micro, us-central1, Ubuntu 26.04), personal Google
  account, project "trading comp". Running since ~Oct 5, 10pm CT.
- No sudo on the VM. Python 3.12 venv via uv at ~/venv.
- Runs via nohup; ~/watchdog.sh on cron every 5 min restarts it (fixed
  Oct 7 after the gaps below).
- Data gaps (no rows recorded; CT = UTC-5):
  - Oct 6 1:48 AM to 1:50 PM CT (06:48 to 18:50 UTC).
  - Oct 7 ~1:00 to 1:13 PM CT (~18:00 to 18:13 UTC).
- Check: tail ~/recorder.log, ls -lh ~/data
- Second recorder (websocket, started Oct 7): record_ws.py writes to
  ~/data_ws/ (ctxws_ ~1s oracle/mark/funding on change, bbo_ best bid/ask
  on every change with exchange time, l2_ full book on change). Log:
  ~/record_ws.log. Also covered by ~/watchdog.sh. Old 5s recorder kept
  running in parallel for comparison.
- Download: tar czf data.tar.gz data, then the SSH window's "Download file".
- About 50 MB/day.

## Strategy direction
- Most teams will use generic AI strategies; our edge should come from
  this competition's specifics:
  - Weekend/overnight oracle freezes and reopen gaps for SP500, EWY, EWJ,
    BRENT, GOLD (crypto trades 24/7).
  - Behavior of the organizer market-making bots (spreads, reaction speed).
  - Scheduled events (CPI, FOMC, BOJ, market opens/closes).
- Play to the scoring: volatility targeting, diversification across
  uncorrelated markets, cutting exposure before gaps and events.
- Report: rigorous out-of-sample testing, baselines, ablations,
  reproducibility; focused ML (regime detection, bet sizing).
- Reliability: the bot must reconnect, handle errors and alert us.

## Experiment log
- REPORT_NOTES.md records every experiment: what we tried, how to
  reproduce it, results, and what we learned, including failures and
  dropped claims. Update it after every step, before committing.

## Week 1 plan (Oct 5-11)
- Roles: infra (me), data/research, strategy/backtesting, risk/report.
- Next: load the historical dataset, build a backtester with fees, run
  baselines (buy-and-hold, equal-risk vol-targeted, simple trend) scored
  on return and daily vol, analyze the first weekend close (Fri) and
  reopen (Mon), set up a private GitHub repo for the team.
