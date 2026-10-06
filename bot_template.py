"""How the bot connects: signs with the API wallet, acts on the TEAM account.

Env vars:
  HL_BASE_URL        endpoint from the guide
  HL_TEAM_ADDRESS    team wallet PUBLIC address
  HL_API_KEY_FILE    path to api_wallet_<name>.key
  HL_DEX             competition exchange (default: par)
"""
import os

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

BASE_URL = os.environ.get("HL_BASE_URL", constants.TESTNET_API_URL)
DEX = os.environ.get("HL_DEX", "par")
TEAM = os.environ["HL_TEAM_ADDRESS"]
with open(os.environ["HL_API_KEY_FILE"]) as f:
    api_wallet = eth_account.Account.from_key(f.read().strip())

info = Info(BASE_URL, skip_ws=True, perp_dexs=[DEX])
# account_address=TEAM: sign with the API wallet, trade on the team account.
# perp_dexs=[DEX]: lets the SDK find the competition markets (par:BTC, par:SP500, ...).
ex = Exchange(api_wallet, BASE_URL, account_address=TEAM, perp_dexs=[DEX])

# All queries use the TEAM address, never the API wallet address
print("Balance:", info.spot_user_state(TEAM)["balances"])
print("Positions on", DEX, ":", info.user_state(TEAM, dex=DEX))
print("Prices:", info.all_mids(dex=DEX))

# Example (commented out): set leverage, then place a limit order
# Market names carry the exchange prefix, e.g. "par:BTC"
# print(ex.update_leverage(5, f"{DEX}:BTC", is_cross=True))
# print(ex.order(f"{DEX}:BTC", True, 0.001, 50000, {"limit": {"tif": "Gtc"}}))
