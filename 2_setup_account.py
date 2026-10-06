"""Step 2: one-time account setup, AFTER organizers fund the team wallet.

Signed by the TEAM wallet:
  - sets unified account mode
  - approves an API wallet (agent) with a name + expiry
Saves the new API wallet key to api_wallet_<name>.key (chmod 600).

Usage:
  export HL_BASE_URL=https://api.hyperliquid-testnet.xyz   # use the URL from the guide
  python 2_setup_account.py --name bot1 --days 30
You'll be prompted for the team wallet private key (hidden input); it is never saved.
"""
import argparse
import getpass
import os
import stat
import time

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

BASE_URL = os.environ.get("HL_BASE_URL", constants.TESTNET_API_URL)

p = argparse.ArgumentParser()
p.add_argument("--name", required=True, help="API wallet name, e.g. bot1 (one per process)")
p.add_argument("--days", type=int, default=30, help="approval expiry in days")
p.add_argument("--skip-unified", action="store_true", help="skip setting unified mode")
args = p.parse_args()

team_key = getpass.getpass("Team wallet private key (hidden): ").strip()
team = eth_account.Account.from_key(team_key)
del team_key
print(f"Team wallet: {team.address}")
print(f"Endpoint:    {BASE_URL}")

info = Info(BASE_URL, skip_ws=True)
ex = Exchange(team, BASE_URL)

# Check funding first
spot = info.spot_user_state(team.address)
print("Balances:", spot.get("balances"))

# 1) Unified mode
if not args.skip_unified:
    print("\nSetting unified account mode...")
    print(ex.user_set_abstraction(team.address, "unifiedAccount"))
print("Account mode:", info.query_user_abstraction_state(team.address))

# 2) Approve API wallet. Hyperliquid encodes the expiry in the agent name
#    as "<name> valid_until <ms timestamp>". Confirm against the guide's
#    "API wallet lifecycle" section.
out = f"api_wallet_{args.name}.key"
if os.path.exists(out):
    raise SystemExit(f"{out} exists. Pick a new --name or remove it deliberately.")
valid_until = int((time.time() + args.days * 86400) * 1000)
agent_name = f"{args.name} valid_until {valid_until}"

print(f"\nApproving API wallet '{agent_name}'...")
resp, agent_key = ex.approve_agent(agent_name)
print("Response:", resp)
if not (isinstance(resp, dict) and resp.get("status") == "ok"):
    raise SystemExit("Approval failed. Nothing saved.")

agent_addr = eth_account.Account.from_key(agent_key).address
fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
with os.fdopen(fd, "w") as f:
    f.write(agent_key)
del agent_key
print(f"API wallet address: {agent_addr}")
print(f"API wallet key saved to {out} (chmod 600)")

# 3) Verify
print("\nApproved API wallets (extraAgents):")
print(info.post("/info", {"type": "extraAgents", "user": team.address}))
