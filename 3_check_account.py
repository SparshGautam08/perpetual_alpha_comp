"""Read-only checks. No private key needed.

Usage: python 3_check_account.py 0xYourTeamWalletAddress
"""
import json
import os
import sys

from hyperliquid.info import Info
from hyperliquid.utils import constants

BASE_URL = os.environ.get("HL_BASE_URL", constants.TESTNET_API_URL)
addr = sys.argv[1]
info = Info(BASE_URL, skip_ws=True)

print("== Account mode ==")
print(json.dumps(info.query_user_abstraction_state(addr), indent=2))
print("== Collateral (spotClearinghouseState) ==")
print(json.dumps(info.spot_user_state(addr), indent=2))
print("== Approved API wallets (extraAgents) ==")
print(json.dumps(info.post("/info", {"type": "extraAgents", "user": addr}), indent=2))
