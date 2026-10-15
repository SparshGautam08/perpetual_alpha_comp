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
- Rules: entries must be algorithmic; >= 1 trade to be ranked (the earlier
  10-day / minimum-volume rules were dropped, Oct 9). No wash trading, self-crossing, collusion,
  or attacks on exchange/oracle. Organizer market-making bots quote in all
  markets. Per-market position limits. Fixed bankroll, no top-ups.
- Outside data IS allowed (confirmed by organizers Oct 7), e.g. CME
  futures, prediction markets, mainnet Hyperliquid data.
- Balances reset when the live competition starts (confirmed Oct 7), so
  pre-competition test trades don't affect the scored bankroll.
- Official site: https://perpetual-alpha26.github.io (docs, FAQ, schedule).
  Contact: perpetual-alpha@googlegroups.com. Part of ICAIF 2026.
- Dates: practice from Oct 5 (non-scored); SCORED PHASE Oct 19 - Nov 6
  (~19 days; >= 1 trade needed to be ranked); report + code due Nov 6;
  winners present at ICAIF Milan, Nov 14-17.
- Fees (official docs): taker 9 bp (matches our Oct 7 test trade),
  maker 3 bp, charged in USDPR per fill.
- Other limits (docs): 50,000 actions/day (unified mode); max 1,000 open
  orders; open-interest cap $500M per market across all teams; max
  leverage BTC/ETH 25-40x, HYPE 10x, SP500/USDEUR/USDJPY 50x, others 20x.
- Prohibited (docs): wash trading/self-crossing, USDPR transfers between
  teams, deliberate-loss value transfer, coordinated/mirrored trading,
  off-market trades, book or price manipulation. Violations = DQ.
  Trading against the market maker's posted quotes is not listed.
- Code is reviewed by the jury for consistency with the report; document
  all external data sources and how they're used (FAQ).
- Organizer answers (Discord #ask-anything, Oct 9-10):
  - Eligibility simplified: only >= 1 trade needed. NO minimum volume and
    NO minimum trading days anymore.
  - LIVE PHASE USES A NEW DEX (name/markets not announced yet). Everything
    must switch from `par` via one setting; re-check market behaviour.
  - Report/code deadline Nov 6. Bot updates allowed during the live phase.
  - Request budget (address-based): 10,000 + 1 per $1 filled volume, not
    reset before Oct 19. Ours on Oct 10: cumVlm $633k, cap 643,568, used 40.
  - Unified mode is capped at 50k actions/day; Standard has no cap. The
    organizers fund the new DEX directly in either mode. To switch to
    Standard: userSetAbstraction -> "disabled" from the TEAM wallet, before
    live funding (only needed for heavy quoting, e.g. a maker strategy).
  - Funding parameters = the matching Hyperliquid mainnet markets.
  - Keyrock's market-making bots were to start during practice "hopefully
    by" Oct 8 (answer Oct 7). Likely the cause of the Oct 8 book change.
  - Orders must be within 80% of markPx (mark stays within +-1/maxLeverage,
    i.e. +-2-5%, of the oracle). Pre-trade capacity: availableToTrade from
    activeAssetData (team address, coin e.g. par:BTC) = free collateral per
    side; maxTradeSzs = availableToTrade x leverage / markPx.
- Unknown: per-market position limits (exist, figures not published);
  new DEX name and markets.

## Account (all set up)
- Endpoint: https://api.hyperliquid-testnet.xyz
- Team wallet (public): 0xC9f1C5a784d2cd5858396299893e71cfe23F4aAf
- Balance: 1,000,000 USDPR. Unified account mode is ON.
- API wallet `bot1` approved: 0x20Cb52dfA3C9934f3ef802FF2D988640532E239C,
  expires ~Nov 5, 2026. WARNING: that is BEFORE the competition ends (Nov
  6). Before Nov 5, move the main bot to another key (bot2, or a new bot3:
  max 3 API wallets per account).
- API wallet `bot2` approved Oct 14: 0x98b0525EE675CC9F1Bf4Da5ae628Af49d11735E3,
  valid until ~Nov 13, 2026. Key ~/keys/api_wallet_bot2.key (laptop). For
  the maker process (one key per running process); not yet on the VM.
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
- balance.py: read-only balance, positions, equity at mark and at oracle.
- check_dex.py: read-only new-DEX readiness check. `--find` lists dexes
  made by the organizers' deployer (0x2457...e211, par's); `check_dex.py
  <name>` shows markets, collateral, leverage, oracle vs mainnet twin,
  team balance there, and prints the settings to use.
- New-DEX switch: ONE setting, HL_DEX (default par), plus HL_COLLATERAL
  (default USDPR) if the collateral token differs, read by bot/config.py,
  balance.py, livefeed (shadow + maker), record_ws.py,
  record_market_data.py. MARKETS if some bot markets are missing. The bot's
  state files get a dex prefix off par (state_<dex>_live_combined.json),
  so par's remembered positions never carry over.
- test_trade.py: small capped test trade (plan/open/status/close/report).
- bot/: trading bot. Paper mode by default: python -m bot.main
  (--rebalance-now, --once). Live needs BOT_LIVE=yes, --mode live and
  HL_API_KEY_FILE. Settings in bot/config.py (env overrides). Kill switch:
  touch bot/state/STOP (flatten + exit); bot/state/PAUSE (no orders).
  Alerts via ntfy (NTFY_TOPIC env). Runs live on the bot VM (see below).
- backtest/: backtester, baselines, leverage study, live market analysis.
- ML Model 1 (oracle forecast): models/oracle_gbm_v1.joblib (+ baseline),
  trained by backtest/train_oracle_model.py; inputs in bot/features.py
  (shared by training and live). bot/livefeed.py = live websocket feed +
  model predictions every second. bot/shadow.py = shadow mode (predictions
  logged, no trading; running on the bot VM since Oct 14 15:24 UTC, log
  ~/shadow.log, data ~/shadow/, ~/shadow_watchdog.sh in cron).
  backtest/eval_shadow.py scores shadow logs.
- bot/mlsignal.py: the live bot reads ML Model 1's latest predictions from
  shadow mode's log (SHADOW_DIR, default ~/shadow; older than 5 s =
  ignored). Drift logs the prediction with every entry ("ML pred ... bp").
  DRIFT_ML_FILTER=yes turns on the E39 filter (entry edge adjusted by the
  predicted oracle move); default off. Needs shadow mode running.
- bot/maker.py: ML maker (post-only quotes around the ML fair price).
  Paper by default (fills simulated from par's trade feed). Live needs its
  OWN key (bot2, see Account): BOT_LIVE=yes HL_MAKER_KEY_FILE=... It
  writes its inventory to bot/state/maker_units_live.json; the combined bot
  excludes it. maker_test.py = the Oct 13 standalone micro-test.
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
  on every change with exchange time, l2_ full book on change, trades_
  every par trade with aggressor side and buyer/seller addresses, since
  Oct 9 23:20 UTC). Log:
  ~/record_ws.log. Also covered by ~/watchdog.sh. Old 5s recorder kept
  running in parallel for comparison.
- Third recorder (Oct 7): record_ws.py --venue mainnet --out ~/data_mainnet
  records the real Hyperliquid markets par copies (BTC, xyz:SP500, ...):
  mn_ctxws_ (oracle/mark/mid/funding ~1s) and mn_bbo_ (best bid/ask price
  changes). Lead signal for par's oracle. ~55 MB/day. Log ~/record_mn.log.
  Watchdog patterns must stay distinct: "record_ws.py --out" (par) vs
  "record_ws.py --venue mainnet".
- Download: tar czf data.tar.gz data, then the SSH window's "Download file".
- About 50 MB/day.

## Bot VM (Google Cloud, started Oct 8)
- VM `bot` (e2-small: 2 vCPU, 2 GB; upgraded from e2-micro 1 GB on Oct 14
  when memory ran low), us-central1, same project as the recorder. Runs the
  bot LIVE 24/7: `--strategy combined` with LEVERAGE=0 since Oct 15 20:55
  UTC (risk parity off, E38; i.e. drift 30 bp only), key bot1. Also runs shadow mode and two paper makers (see Current status).
- Code: ~/perpetual_alpha_comp, cloned with a READ-ONLY deploy key
  (~/.ssh/deploy_key, GitHub deploy key "bot VM (read-only)"). Python venv
  ~/venv (uv), installed from requirements.txt.
- Key: ~/keys/api_wallet_bot1.key (chmod 600). Never put team_wallet.key on
  any server.
- ~/run_bot.sh sets HL_API_KEY_FILE, NTFY_TOPIC, BOT_LIVE and starts the
  bot; log ~/bot.log. Since Oct 13 00:10 UTC it also sets LOOP_SECONDS=15,
  DRIFT_ENTRY_BP=30 (v2b) and LEVERAGE=0.25 (risk parity at half size), and
  runs `--strategy combined`. ~/bot_watchdog.sh (cron every 5 min) restarts it
  unless bot/state/STOP exists.
- Check: tail ~/bot.log. Stop + flatten: touch
  ~/perpetual_alpha_comp/bot/state/STOP (watchdog then leaves it stopped;
  rm the file to allow restarts). Update code: cd ~/perpetual_alpha_comp
  && git pull && pkill -f "bot.main --mode live" (watchdog restarts it
  within 5 min with the new code).
- Only ONE live bot per account: don't run it on a laptop at the same time.

## Current status (Oct 15) and next steps
- Scored phase starts Oct 19 (new DEX, name not announced). Practice money
  resets then. Equity at oracle ~992,900 on Oct 14 (practice).
- Running on the bot VM: live bot, drift 30 bp only (+ 200 bp catastrophe
  stop; risk parity OFF since Oct 15 20:55 UTC, LEVERAGE=0 in
  ~/run_bot.sh, backup ~/run_bot.sh.bak), shadow mode (ML predictions logged to
  ~/shadow/), and two PAPER makers (`--fair gbm` and `--fair base`, logs
  ~/maker_*.log, fills/markouts in
  ~/perpetual_alpha_comp/bot/state/maker_paper_*_*.csv), restarted Oct 15
  with the replay-trade fix (E36). Makers have no watchdog; restart with
  `cd ~/perpetual_alpha_comp && nohup ~/venv/bin/python -m bot.maker
  --fair gbm >> ~/maker_gbm.log 2>&1 &` (same for base).
- Scoring runs ON the VM (the browser SSH download fails on big files):
  `~/venv/bin/python -m backtest.eval_shadow ~/shadow` and
  `~/venv/bin/python -m backtest.eval_maker <folder with maker CSVs +
  shadow CSVs>` (e.g. copy both into ~/eval first).
- Key results so far: drift v2/v2b passed locked tests (E27); at live
  check speed (15 s) drift 30 bp made ~$550/day in backtest (E30-E31);
  weekend oracle does not freeze (E28); ML Model 1 live in shadow mode for
  29 h: R2 0.358 vs baseline 0.306, better in all 10 markets (E35); paper
  maker roughly break-even after fees (+0.7 to +2.0 bp, within noise) and
  the ML fair price did not beat the simple one (E36): no live maker as
  configured; risk parity adds daily swings but almost no return:
  backtest score is best with it OFF (E38), so it was switched off.
  ML filter for drift entries passed its locked test narrowly (+$99 on 37
  trades, E39); built into the bot, OFF until a second check on live data.
- New-DEX switch ready (E37). On announcement: `python check_dex.py
  --find`, then `python check_dex.py <name>`; on the bot VM add
  `export HL_DEX=<name>` (and HL_COLLATERAL if shown) to ~/run_bot.sh and
  restart; restart shadow/makers/recorders with HL_DEX set too.
- Next: (1) ~Oct 18: second check of the E39 ML filter on live drift
  entries since Oct 15 (bot.log "ML pred" + outcomes); turn on with
  DRIFT_ML_FILTER=yes in ~/run_bot.sh only if it agrees; (2) switch to
  the new DEX when announced (see above); (3) move the main bot off bot1
  before Nov 5 (bot2 is free: no live maker); (4) back up the recorded
  data (e.g. a Cloud Storage bucket) before Nov 6; (5) start the report
  (REPORT_NOTES.md has all experiments E1-E39).

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
- Done (historical data, backtester, baselines, weekend analysis, team repo).
