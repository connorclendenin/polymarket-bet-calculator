"""Automated paper-trading tracker: several independent simulated bankrolls,
each running a clearly different, pre-registered strategy (see STRATEGIES)
against this app's own signals, settling each bet against the real game
outcome on Polymarket. No real money, no real orders; this never touches
Polymarket beyond reading public market data.

Why several strategies instead of one: with only a handful of settled bets,
no amount of after-the-fact tuning of ONE strategy's thresholds/staking can
honestly tell you it's "found" a winner — that's just fitting noise. The
only honest way to find out which approach actually holds up is to run a
few clearly different rule sets side by side against the exact same signal
stream and let real settled outcomes accumulate under each, over weeks, not
one sitting. STRATEGIES below are intentionally few and clearly
differentiated (selectivity vs. staking), not a tuned grid search.
"""

import uuid
from collections import Counter
from datetime import datetime, timezone

import confidence
import polymarket
import store

STARTING_BALANCE = 100.0
FLAT_STAKE = 5.0  # same stake every bet, so calibration isn't confounded by sizing
MIN_BALANCE_TO_BET = 1.0

# Caps how many simultaneous pending bets any one league can hold, so a
# league that happens to throw off an unusually large batch of simultaneous
# high-confidence signals on a given night (observed in practice: NHL on a
# heavy slate) can't eat the whole bankroll before other sports' signals —
# or NFL prop bets — get a turn. Existing bets already placed before this
# cap existed aren't touched; it only gates new placements going forward.
MAX_PENDING_PER_LEAGUE = 4

CONFIDENCE_BUCKETS = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]

# A (signal_type, league) combo only gets paused once it's lost money across
# enough settled bets that it looks like a real pattern, not one bad beat —
# below this count nothing is ever paused, however it's done so far.
MIN_SEGMENT_SAMPLES = 10

# "Odds swing" — a genuinely different strategy from the four above: not
# hold-to-resolution at all, but buy a deep longshot cheap and sell it back
# the moment the market re-prices it higher (classic momentum/mean-reversion
# trading), riding to the real game resolution only if that never happens.
# Connor's own explicit ask, including the exact bound: try it 5 times total
# and stop — not an ongoing strategy like the other four, a one-shot trial.
ODDS_SWING_STRATEGY_ID = "odds_swing"
ODDS_SWING_ENTRY_MIN = 0.03  # "buy low" — deep longshots only, 3-4 cents
ODDS_SWING_ENTRY_MAX = 0.04
ODDS_SWING_SELL_MULTIPLE = 2.0  # "sell high" — exits once the price has at
# least doubled from entry (e.g. 3c -> 6c+). Hand-picked, not fit to data:
# meaningful enough to not trigger on every tiny wiggle, low enough to
# actually be reachable within 5 trials on a real swing (momentum/an early
# score in a live game is exactly the kind of move that can do this).
ODDS_SWING_MAX_TRIALS = 5  # total trades ever, win or lose — once reached,
# this strategy stops placing new bets permanently (it does NOT reset).


def _kelly_stake(state, signal):
    """Quarter-Kelly, sized off THIS strategy's own paper balance — reuses
    the exact same formula/caps already built for real-money bet sizing
    (confidence.py), just applied to a simulated bankroll instead."""
    if state["balance"] < confidence.MIN_MEANINGFUL_BALANCE:
        return 0.0
    fraction = min(
        confidence._kelly_fraction(signal["confidence"], signal["market_price"]),
        confidence.MAX_STAKE_PCT_OF_BALANCE,
    )
    return round(fraction * state["balance"], 4)


# Each strategy runs against the exact same incoming signals (this app's
# existing underdog/lock/prop thresholds already applied upstream in
# confidence.py — a strategy here can only ADD a stricter filter on top,
# never loosen below what confidence.py already required, since anything
# looser was already discarded before reaching paper_trading at all).
STRATEGIES = {
    "baseline": {
        "label": "Baseline — current thresholds, flat $5 stake",
        "filter_underdog": lambda s: True,
        "filter_lock": lambda s: True,
        "filter_prop": lambda s: True,
        "stake_for": lambda state, s: FLAT_STAKE,
    },
    "high_conviction": {
        "label": "High conviction — stricter thresholds, flat $5 stake",
        "filter_underdog": lambda s: s["edge"] >= 0.15,
        "filter_lock": lambda s: s["confidence"] >= 0.90,
        "filter_prop": lambda s: s["edge"] >= 0.15,
        "stake_for": lambda state, s: FLAT_STAKE,
    },
    "kelly_sized": {
        "label": "Kelly-sized — same signals as baseline, stake scaled by edge",
        "filter_underdog": lambda s: True,
        "filter_lock": lambda s: True,
        "filter_prop": lambda s: True,
        "stake_for": _kelly_stake,
    },
}


def _default_state():
    return {"starting_balance": STARTING_BALANCE, "balance": STARTING_BALANCE, "bets": []}


def _state_keys(strategy_id):
    """(redis_key, local_key) for one strategy's state — "baseline" keeps
    the original local filename (`paper_trades.json`, no `.json` suffix
    duplicated by store.py) so its existing history isn't orphaned."""
    local_key = "paper_trades" if strategy_id == "baseline" else f"paper_trades_{strategy_id}"
    return f"polymarket-bet-calculator:paper_trades:{strategy_id}", local_key


def _load_state(strategy_id="baseline"):
    redis_key, local_key = _state_keys(strategy_id)
    return store.load(redis_key, local_key, _default_state())


def _save_state(state, strategy_id="baseline"):
    redis_key, local_key = _state_keys(strategy_id)
    store.save(redis_key, local_key, state)


def _already_bet(state, event_id, outcome):
    return any(b["event_id"] == event_id and b["outcome"] == outcome for b in state["bets"])


def add_funds(amount, strategy_id="baseline"):
    """Tops up one strategy's paper bankroll without touching existing
    bets/history — e.g. when it's run low on uncommitted cash but there are
    fresh signals worth tracking right now."""
    state = _load_state(strategy_id)
    state["balance"] = round(state["balance"] + amount, 4)
    _save_state(state, strategy_id)
    return state["balance"]


def _place_one(state, row, signal, signal_type, stake=None):
    price = signal.get("market_price")
    if not price or price <= 0:
        return None
    stake = FLAT_STAKE if stake is None else stake
    stake = min(stake, state["balance"])
    if stake <= 0:
        return None
    shares = round(stake / price, 4)
    bet = {
        "id": uuid.uuid4().hex[:10],
        "event_id": row["event_id"],
        "league": row["league"],
        "title": row["title"],
        "outcome": signal["outcome"],
        # What settle_pending_bets compares the real winning side against.
        # Equal to `outcome` for moneyline bets (team name); for prop bets
        # `outcome` is a human-readable label ("Game total Over 46.5") but
        # the real market only ever resolves to "Over"/"Under", so prop
        # signals supply this separately.
        "settlement_key": signal.get("settlement_key", signal["outcome"]),
        "token_id": signal.get("token_id"),  # prop bets only — which of the
        # ladder's many parallel total-line markets this actually is.
        "signal_type": signal_type,  # "underdog" | "lock" | "prop"
        "basis": signal.get("basis"),
        "confidence": signal["confidence"],
        "price": price,
        "stake": round(stake, 4),
        "shares": shares,
        "placed_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "settled_at": None,
        "payout": None,
        "pnl": None,
        "polymarket_url": row.get("polymarket_url"),
    }
    state["balance"] = round(state["balance"] - stake, 4)
    state["bets"].append(bet)
    return bet


def _normalize_prop_signal(prop_bet):
    """Adapts a row's `prop_bet` dict (confidence.attach_nfl_props's shape)
    into the outcome/confidence/market_price shape `_place_one` expects,
    same as underdog_bet/lock already are."""
    return {
        "outcome": f"{prop_bet['market']} {prop_bet['side']} {prop_bet['line']}",
        "settlement_key": prop_bet["side"],
        "confidence": prop_bet["model_prob"],
        "market_price": prop_bet["market_price"],
        "basis": "prop",
        "token_id": prop_bet["token_id"],
        "edge": prop_bet["edge"],
    }


def _segment_performance(settled_bets):
    """Win rate and net P/L per (signal_type, league) combo — the unit this
    app can actually act on ("stop making NHL lock bets", not "stop trusting
    87% confidence"). A segment is flagged `paused` once it has lost money
    across MIN_SEGMENT_SAMPLES+ settled bets — enough tries that a real
    pattern, not one bad beat, is more likely than not."""
    agg = {}
    for b in settled_bets:
        key = (b["signal_type"], b["league"])
        a = agg.setdefault(key, {"count": 0, "wins": 0, "pnl": 0.0})
        a["count"] += 1
        a["pnl"] += b["pnl"]
        if b["status"] == "won":
            a["wins"] += 1

    result = []
    for (signal_type, league), a in sorted(agg.items()):
        result.append(
            {
                "signal_type": signal_type,
                "league": league,
                "count": a["count"],
                "win_rate": round(a["wins"] / a["count"], 4),
                "pnl": round(a["pnl"], 4),
                "paused": a["count"] >= MIN_SEGMENT_SAMPLES and a["pnl"] < 0,
            }
        )
    return result


def _run_strategy(strategy_id, cfg, underdog_rows, lock_rows, prop_rows):
    """Places new bets for one strategy against the current signal set,
    subject to the same two gates every strategy gets regardless of its own
    filter/stake rules:
    - a (signal_type, league) combo currently flagged `paused` by this
      strategy's own _segment_performance (it has lost money across enough
      of ITS OWN settled bets to trust the pattern — each strategy's pause
      state is independent, since they place different bets), and
    - a league that already holds MAX_PENDING_PER_LEAGUE of this strategy's
      own pending bets, so one league's glut of signals can't crowd out
      every other sport within this strategy's bankroll.
    Settles this strategy's pending bets first, so placement decisions use
    the freshest balance/history available. Idempotent — safe to call on
    every scan."""
    state = settle_pending_bets(strategy_id)
    placed = []
    settled = [b for b in state["bets"] if b["status"] != "pending"]
    paused = {
        (s["signal_type"], s["league"]) for s in _segment_performance(settled) if s["paused"]
    }
    pending_by_league = Counter(b["league"] for b in state["bets"] if b["status"] == "pending")

    def _try_place(row, signal, signal_type):
        """Returns True if the caller should stop processing further rows
        in this list (bankroll exhausted) — False otherwise, whether or not
        a bet actually got placed."""
        if state["balance"] < MIN_BALANCE_TO_BET:
            return True
        if _already_bet(state, row["event_id"], signal["outcome"]):
            return False
        if (signal_type, row["league"]) in paused:
            return False
        if pending_by_league[row["league"]] >= MAX_PENDING_PER_LEAGUE:
            return False
        stake = cfg["stake_for"](state, signal)
        bet = _place_one(state, row, signal, signal_type, stake)
        if bet:
            placed.append(bet)
            pending_by_league[row["league"]] += 1
        return False

    for row in underdog_rows:
        signal = row.get("underdog_bet")
        if signal and cfg["filter_underdog"](signal) and _try_place(row, signal, "underdog"):
            break

    for row in lock_rows:
        signal = row.get("lock")
        if signal and cfg["filter_lock"](signal) and _try_place(row, signal, "lock"):
            break

    for row in prop_rows:
        prop = row.get("prop_bet")
        if not prop:
            continue
        signal = _normalize_prop_signal(prop)
        if cfg["filter_prop"](signal) and _try_place(row, signal, "prop"):
            break

    if placed:
        _save_state(state, strategy_id)
    return placed


def _odds_swing_candidates(rows):
    """Any moneyline outcome currently priced in the deep-longshot entry
    band — deliberately NOT edge-based, no model involved at all, unlike
    every other signal in this app. This is a pure price-action play: the
    model's opinion doesn't matter, only where the market itself has priced
    a team right now. Scoped to moneyline only (row['outcomes'], the two
    game-winner sides) — doesn't touch prop/total markets.

    Live games only — Connor's explicit correction: this is about in-game
    momentum swings (a score, a turnover re-pricing a team fast), not
    pregame price drift. A pregame longshot sitting at 3-4% days out is a
    completely different thing (just a bad team), not what this strategy
    is for."""
    candidates = []
    for row in rows:
        if row.get("status") != "live":
            continue
        for outcome in row.get("outcomes", []):
            price = outcome.get("best_ask")
            token_id = outcome.get("token_id")
            if price is None or token_id is None:
                continue
            if ODDS_SWING_ENTRY_MIN <= price <= ODDS_SWING_ENTRY_MAX:
                candidates.append(
                    {
                        "event_id": row["event_id"],
                        "league": row["league"],
                        "title": row["title"],
                        "outcome": outcome["name"],
                        "token_id": token_id,
                        "price": price,
                        "polymarket_url": row.get("polymarket_url"),
                    }
                )
    return candidates


def run_odds_swing_strategy(rows):
    """Buy-low-sell-high on LIVE games only, bounded to ODDS_SWING_MAX_TRIALS
    total trades ever (Connor's explicit ask: try it 5 times, see if it
    works, stop either way — and only on in-game momentum swings, not
    pregame price drift). Settles pending bets first — each one either
    sells immediately once the live order book shows its sell target hit,
    or (if not) rides along to the real game resolution exactly like every
    other bet in this app — then places new entries, capped by the trial
    limit, not by balance alone (so it can't quietly use up its 5 tries
    faster than intended just because the bankroll allows more)."""
    state = _load_state(ODDS_SWING_STRATEGY_ID)
    changed = False

    pending = [b for b in state["bets"] if b["status"] == "pending"]
    if pending:
        token_ids = [b["token_id"] for b in pending if b.get("token_id")]
        try:
            books = polymarket.fetch_books(token_ids) if token_ids else {}
        except Exception:
            books = {}
        for bet in pending:
            book = books.get(bet.get("token_id")) or {}
            current_bid = book.get("best_bid")
            if current_bid is not None and current_bid >= bet["price"] * ODDS_SWING_SELL_MULTIPLE:
                payout = round(bet["shares"] * current_bid, 4)
                bet["status"] = "sold"
                bet["settled_at"] = datetime.now(timezone.utc).isoformat()
                bet["payout"] = payout
                bet["pnl"] = round(payout - bet["stake"], 4)
                bet["exit_price"] = current_bid
                state["balance"] = round(state["balance"] + payout, 4)
                changed = True
                continue
            # Sell target not hit yet — did the real game resolve first?
            try:
                winner = polymarket.fetch_event_resolution(bet["event_id"])
            except Exception:
                continue
            if winner is None:
                continue
            won = winner == bet["settlement_key"]
            payout = round(bet["shares"], 4) if won else 0.0
            bet["status"] = "won" if won else "lost"
            bet["settled_at"] = datetime.now(timezone.utc).isoformat()
            bet["payout"] = payout
            bet["pnl"] = round(payout - bet["stake"], 4)
            state["balance"] = round(state["balance"] + payout, 4)
            changed = True

    placed = []
    if len(state["bets"]) < ODDS_SWING_MAX_TRIALS and state["balance"] >= MIN_BALANCE_TO_BET:
        for candidate in _odds_swing_candidates(rows):
            if len(state["bets"]) >= ODDS_SWING_MAX_TRIALS:
                break
            if _already_bet(state, candidate["event_id"], candidate["outcome"]):
                continue
            signal = {
                "outcome": candidate["outcome"],
                "settlement_key": candidate["outcome"],
                "confidence": candidate["price"],  # no model here — this IS the market's own number
                "market_price": candidate["price"],
                "basis": "odds_swing",
                "token_id": candidate["token_id"],
            }
            bet = _place_one(state, candidate, signal, "odds_swing", FLAT_STAKE)
            if bet:
                placed.append(bet)
                changed = True

    if changed:
        _save_state(state, ODDS_SWING_STRATEGY_ID)
    return placed


def run_all_strategies(underdog_rows, lock_rows, prop_rows=None):
    """Runs every registered strategy (STRATEGIES) against the same signal
    set — the actual "keep searching for a winning strategy" loop: each
    strategy accumulates its own real settlement history independently, so
    comparing them (get_strategy_comparison) over enough settled bets is
    what actually answers the question, not any single tuning pass."""
    prop_rows = prop_rows or []
    placed = {}
    for strategy_id, cfg in STRATEGIES.items():
        placed[strategy_id] = _run_strategy(strategy_id, cfg, underdog_rows, lock_rows, prop_rows)
    return placed


def settle_pending_bets(strategy_id="baseline"):
    state = _load_state(strategy_id)
    changed = False
    for bet in state["bets"]:
        if bet["status"] != "pending":
            continue
        try:
            if bet["signal_type"] == "prop":
                winner = polymarket.fetch_prop_resolution(bet["event_id"], bet["token_id"])
            else:
                winner = polymarket.fetch_event_resolution(bet["event_id"])
        except Exception:
            continue
        if winner is None:
            continue
        won = winner == bet.get("settlement_key", bet["outcome"])
        payout = round(bet["shares"], 4) if won else 0.0
        bet["status"] = "won" if won else "lost"
        bet["settled_at"] = datetime.now(timezone.utc).isoformat()
        bet["payout"] = payout
        bet["pnl"] = round(payout - bet["stake"], 4)
        state["balance"] = round(state["balance"] + payout, 4)
        changed = True
    if changed:
        _save_state(state, strategy_id)
    return state


def get_summary(strategy_id="baseline"):
    state = settle_pending_bets(strategy_id)  # always catch up settlements before reporting
    bets = state["bets"]
    settled = [b for b in bets if b["status"] != "pending"]
    pending = [b for b in bets if b["status"] == "pending"]
    # A profitable early "sold" exit (the odds-swing strategy) is as much a
    # win as a resolved "won" bet — for every status the other three
    # strategies ever produce, pnl > 0 and status == "won" are identical
    # (a resolved bet's payout is either the full share value or zero), so
    # this doesn't change their numbers at all, just correctly includes
    # "sold" too.
    wins = [b for b in settled if b["pnl"] > 0]

    total_pnl = round(sum(b["pnl"] for b in settled), 4)
    win_rate = round(len(wins) / len(settled), 4) if settled else None

    history = [
        {"timestamp": None, "balance": state["starting_balance"], "label": "Start"}
    ]
    running = state["starting_balance"]
    for b in sorted(settled, key=lambda x: x["settled_at"]):
        running = round(running + b["pnl"], 4)
        history.append(
            {
                "timestamp": b["settled_at"],
                "balance": running,
                "label": f"{b['title']} — {b['outcome']} ({b['status']})",
            }
        )

    calibration = []
    for lo, hi in CONFIDENCE_BUCKETS:
        bucket = [b for b in settled if lo <= b["confidence"] < hi]
        if not bucket:
            continue
        actual = sum(1 for b in bucket if b["pnl"] > 0) / len(bucket)
        avg_conf = sum(b["confidence"] for b in bucket) / len(bucket)
        calibration.append(
            {
                "range": f"{int(lo * 100)}-{int(min(hi, 1.0) * 100)}%",
                "avg_predicted_confidence": round(avg_conf, 4),
                "actual_win_rate": round(actual, 4),
                "count": len(bucket),
            }
        )

    return {
        "starting_balance": state["starting_balance"],
        "balance": round(state["balance"], 4),
        "total_pnl": total_pnl,
        "win_rate": win_rate,
        "settled_count": len(settled),
        "pending_count": len(pending),
        "history": history,
        "calibration": calibration,
        "segment_performance": _segment_performance(settled),
        "bets": sorted(bets, key=lambda b: b["placed_at"], reverse=True),
    }


def get_strategy_comparison():
    """The actual answer to "which strategy works": each registered
    strategy's real P/L and ROI against its own independent paper bankroll,
    side by side. Early on (few settled bets per strategy) this will look
    like noise — that's correct and expected, not a bug; a meaningful gap
    only means something once settled_count is comfortably into the tens
    per strategy. Don't declare a winner off this before then."""
    comparison = []
    for strategy_id, cfg in STRATEGIES.items():
        s = get_summary(strategy_id)
        starting = s["starting_balance"]
        roi_pct = round((s["balance"] - starting) / starting * 100, 2) if starting else None
        comparison.append(
            {
                "strategy_id": strategy_id,
                "label": cfg["label"],
                "balance": s["balance"],
                "roi_pct": roi_pct,
                "total_pnl": s["total_pnl"],
                "win_rate": s["win_rate"],
                "settled_count": s["settled_count"],
                "pending_count": s["pending_count"],
            }
        )

    # Odds-swing isn't in STRATEGIES — different mechanic (early exit on
    # price, not hold-to-resolution) and a bounded one-shot trial rather
    # than an ongoing strategy — but still belongs in the comparison view.
    s = get_summary(ODDS_SWING_STRATEGY_ID)
    starting = s["starting_balance"]
    roi_pct = round((s["balance"] - starting) / starting * 100, 2) if starting else None
    trials_used = s["settled_count"] + s["pending_count"]
    comparison.append(
        {
            "strategy_id": ODDS_SWING_STRATEGY_ID,
            "label": (
                f"Odds swing — buy {int(ODDS_SWING_ENTRY_MIN*100)}-{int(ODDS_SWING_ENTRY_MAX*100)}%, "
                f"sell at {ODDS_SWING_SELL_MULTIPLE}x (trial: {trials_used}/{ODDS_SWING_MAX_TRIALS})"
            ),
            "balance": s["balance"],
            "roi_pct": roi_pct,
            "total_pnl": s["total_pnl"],
            "win_rate": s["win_rate"],
            "settled_count": s["settled_count"],
            "pending_count": s["pending_count"],
        }
    )

    comparison.sort(key=lambda c: (c["roi_pct"] is None, -(c["roi_pct"] or 0)))
    return comparison
