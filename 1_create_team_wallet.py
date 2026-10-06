"""Step 1: create a brand-new team wallet (run ONCE, on your own machine).

Prints the public address (send this to the organizers) and writes the
private key to team_wallet.key with owner-only permissions. Move that file
into your password manager / offline storage afterwards. It must NOT live
next to the bot.
"""
import os
import stat
import eth_account

OUT = "team_wallet.key"
if os.path.exists(OUT):
    raise SystemExit(f"{OUT} already exists. Refusing to overwrite an existing key.")

acct = eth_account.Account.create()
fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
with os.fdopen(fd, "w") as f:
    f.write(acct.key.hex() if acct.key.hex().startswith("0x") else "0x" + acct.key.hex())

print("Team wallet PUBLIC address (send this to the organizers):")
print(acct.address)
print(f"\nPrivate key saved to {OUT} (chmod 600). Back it up securely and never share it.")
