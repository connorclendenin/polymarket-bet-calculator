"""Reconstructs a realized P/L history from a wallet's Polymarket activity
feed. Polymarket has no dedicated P/L-history endpoint, so this is built
from the same trade/redemption/rebate log the site itself is built on.

Important: this is REALIZED cash flow only. Buying a position is a cash
outflow recorded immediately; it only comes back (REDEEM) if and when that
market resolves in your favor. An open, unresolved position therefore shows
as a standing dip in this series until it settles — it is not a loss, just
money not yet returned. Add the current open-positions value (already shown
in the positions panel) to the latest point here for the full picture.
"""

from datetime import datetime, timezone

# Cash moving INTO the wallet from Polymarket's own ledger.
CASH_IN_TYPES = {"REDEEM", "MAKER_REBATE", "TAKER_REBATE"}


def build_pnl_series(activity):
    events = sorted(activity, key=lambda a: a.get("timestamp", 0))
    points = []
    running = 0.0

    for a in events:
        usdc = a.get("usdcSize") or 0
        a_type = a.get("type")

        if a_type == "TRADE":
            side = a.get("side")
            if side == "BUY":
                running -= usdc
            elif side == "SELL":
                running += usdc
            else:
                continue
        elif a_type in CASH_IN_TYPES:
            running += usdc
        else:
            continue  # unrecognized activity type; skip rather than guess

        points.append(
            {
                "timestamp": a["timestamp"],
                "date": datetime.fromtimestamp(a["timestamp"], tz=timezone.utc).isoformat(),
                "cumulative_pnl": round(running, 4),
                "label": a.get("title") or a_type,
                "type": a_type,
            }
        )

    return points


# --- polymarket.us account: capital-flow reconstruction ---
#
# polymarket.us's /v1/portfolio/activities does NOT cleanly label which side
# of a matched trade belongs to the account (aggressor vs. passive execution
# are both included in full), so there's no reliable way to sign a trade's
# cash flow without risking getting it backwards on real money. Rather than
# guess, this only reconstructs from the activity types where the cash
# direction is unambiguous: deposits, withdrawals, referral bonuses, fee
# rebates. TRANSFER activities carry no direction field either and are
# excluded too. This is therefore a CAPITAL FLOW line (what you put in and
# took out), not a trade-by-trade portfolio value curve — it can legitimately
# go negative (if you've withdrawn more than you've deposited, funded by
# real trading gains not reflected in this line) and it will not match the
# account's actual value at any point except the final one, which is always
# anchored to the real, verified current balance rather than derived.

US_CASH_IN_TYPES = {
    "ACTIVITY_TYPE_ACCOUNT_DEPOSIT",
    "ACTIVITY_TYPE_ACCOUNT_ADVANCED_DEPOSIT",
    "ACTIVITY_TYPE_REFERRAL_BONUS",
    "ACTIVITY_TYPE_TAKER_FEE_REBATE",
    "ACTIVITY_TYPE_LIQUIDITY_PROGRAM",
}
US_CASH_OUT_TYPES = {"ACTIVITY_TYPE_ACCOUNT_WITHDRAWAL"}


def build_us_capital_series(activities, current_balance):
    events = sorted(
        (a for a in activities if a.get("type") in US_CASH_IN_TYPES | US_CASH_OUT_TYPES),
        key=lambda a: a["accountBalanceChange"]["updateTime"],
    )

    points = []
    running = 0.0
    for a in events:
        bc = a["accountBalanceChange"]
        amount = float(bc["amount"]["value"])
        if a["type"] in US_CASH_OUT_TYPES:
            amount = -amount
        running = round(running + amount, 4)
        points.append(
            {
                "timestamp": bc["updateTime"],
                "net_capital": running,
                "label": a["type"].replace("ACTIVITY_TYPE_", "").replace("_", " ").title(),
            }
        )

    total_net_capital = running
    overall_return_pct = None
    if current_balance is not None and total_net_capital:
        overall_return_pct = round((current_balance - total_net_capital) / total_net_capital * 100, 2)

    points.append(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "net_capital": running,
            "current_balance": round(current_balance, 4) if current_balance is not None else None,
            "label": "Now (actual balance)",
        }
    )

    return {
        "points": points,
        "total_net_capital": round(total_net_capital, 4),
        "overall_return_pct": overall_return_pct,
    }
