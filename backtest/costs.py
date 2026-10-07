"""Default cost assumptions. Edit these as better numbers come in.

All PROVISIONAL: measured from ~40 minutes of par data (Oct 6, 02:05-02:45
UTC, data/ctx_2026-10-06.csv and data/book_2026-10-06.csv). Replace with
the server recordings once analyzed.
"""

# Exchange fee per side, in bp of traded notional. Hyperliquid's base taker
# fee is 4.5 bp; par's actual fee is unknown (asked the organizers).
FEE_BPS = 5.0

# Half the bid-ask spread, in bp: what crossing the book costs vs the mid.
# The organizer market-making bots quote ~20 bp wide in every market.
HALF_SPREAD_BPS = {
    "BTC": 10.0, "ETH": 10.0, "HYPE": 10.0, "SP500": 10.0, "EWY": 10.0,
    "EWJ": 10.0, "BRENT": 10.0, "GOLD": 10.0, "USDEUR": 10.0, "USDJPY": 10.0,
}

# Funding as an annual rate on position notional. Positive = longs pay
# shorts, negative = shorts pay longs (Hyperliquid convention, paid hourly).
# A constant rate is a crude stand-in: real funding moves with the premium,
# so do not trust strategies whose edge is mostly "collect funding".
FUNDING_APR = {
    "BTC": 0.11, "ETH": 0.11, "HYPE": 0.44, "SP500": 0.09, "EWY": 0.05,
    "EWJ": -0.25, "BRENT": 0.07, "GOLD": 0.09, "USDEUR": 0.09, "USDJPY": 0.33,
}
