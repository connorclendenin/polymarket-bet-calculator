"""Automated paper-trading tracker: three independent simulated $100
bankrolls, each running a clearly different, pre-registered strategy (see
STRATEGIES) against this app's own signals, settling each bet against the
real game outcome on Polymarket. No real money, no real orders; this never
touches Polymarket beyond reading public market data.

The three accounts, Connor's own explicit design:
- `underdogs` — only "Smart underdog" signals, flat stake.
- `locks` — only "Lock" signals, flat stake.
- `combined` — both signal types, but stake sizing *learns from this
  account's own settled history*: a (signal_type, league) segment that's
  been winning gets a bigger stake next time, one that's been losing gets a
  smaller one, well before a real loser gets fully paused (see
  _segment_performance / _combined_adaptive_stake). This is the account
  that's actually supposed to refine its own bet choices over time, not
  just report calibration after the fact.

Why three separate accounts instead of one: with only a handful of settled
bets, no amount of after-the-fact tuning of ONE strategy's thresholds can
honestly tell you it's "found" a winner — that's just fitting noise. Running
clearly different rule sets side by side against the exact same signal
stream and letting real settled outcomes accumulate under each, over weeks,
is the only honest way to find out which approach — and whether adapting at
all — actually holds up.
"""

import uuid
from collections import Counter
from datetime import datetime, timezone

import polymarket
import store

STARTING_BALANCE = 100.0
FLAT_STAKE = 5.0  # same stake every bet, so calibration isn't confounded by sizing
MIN_BALANCE_TO_BET = 1.0

# Connor's explicit ask: he trades basketball, NFL, and CFB predominantly,
# so paper trading (all strategies) should only ever place NEW bets in these
# leagues — not the whole app, which still scans/shows every league on the
# main dashboard. "Basketball" covers both NBA and CBB. Scoping is applied
# by filtering the already-scanned rows before signals are derived from
# them, not by re-scanning — cheap, no extra API calls. Existing pending
# bets already placed in other leagues (MLB/NHL) aren't cancelled — they
# still settle normally; this only gates new placements.
PAPER_TRADING_LEAGUES = {"nfl", "nba", "cbb", "cfb"}


def _restrict_to_paper_trading_leagues(rows):
    return [r for r in rows if r["league"] in PAPER_TRADING_LEAGUES]


def _soonest_first(rows):
    """Connor's explicit ask: he doesn't want to keep waiting on results, so
    when there are more eligible signals than a strategy can act on (the
    per-league pending cap below), the games that actually get bet on
    should be the ones starting soonest — today's games, not ones days
    out — so bets resolve and feed the calibration/learning loop faster.
    Rows with no parseable start time sort last, never first."""
    return sorted(rows, key=lambda r: r.get("game_start") or "9999")

# Caps how many simultaneous pending bets any one league can hold, so a
# league that happens to throw off an unusually large batch of simultaneous
# high-confidence signals on a given night (observed in practice: NHL on a
# heavy slate) can't eat the whole bankroll before other sports' signals get
# a turn. Existing bets already placed before this cap existed aren't
# touched; it only gates new placements going forward.
MAX_PENDING_PER_LEAGUE = 4

CONFIDENCE_BUCKETS = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]

# A (signal_type, league) combo only gets paused once it's lost money across
# enough settled bets that it looks like a real pattern, not one bad beat —
# below this count nothing is ever paused, however it's done so far.
MIN_SEGMENT_SAMPLES = 10

# `combined`'s own learning knobs. Below COMBINED_MIN_SAMPLES_TO_ADAPT
# settled bets in a (signal_type, league) segment there's no real track
# record yet, so it bets at the flat baseline; above that, stake scales
# with that segment's own win rate — win_rate 0.5 (coin flip) -> 1.0x
# (baseline), 0.75 -> 1.5x, 0.25 -> 0.5x, clamped to the two bounds below.
# This is deliberately a small, inspectable rule (not a trained model): bet
# bigger where this account has actually been right, smaller where it
# hasn't, and let MIN_SEGMENT_SAMPLES's existing hard pause (above) take
# over entirely once a segment is a confirmed loser.
COMBINED_MIN_SAMPLES_TO_ADAPT = 5
COMBINED_MIN_STAKE_MULT = 0.4
COMBINED_MAX_STAKE_MULT = 1.75


def _flat_stake(state, signal, row, signal_type):
    return FLAT_STAKE


def _combined_adaptive_stake(state, signal, row, signal_type):
    """`combined`'s own stake sizing: learns from this account's settled
    history by scaling the stake to how well this exact (signal_type,
    league) segment has performed so far, before a confirmed loser gets
    fully paused upstream (_run_strategy's MIN_SEGMENT_SAMPLES gate)."""
    settled = [b for b in state["bets"] if b["status"] != "pending"]
    segment = next(
        (
            s
            for s in _segment_performance(settled)
            if s["signal_type"] == signal_type and s["league"] == row["league"]
        ),
        None,
    )
    if segment is None or segment["count"] < COMBINED_MIN_SAMPLES_TO_ADAPT:
        return FLAT_STAKE
    edge = segment["win_rate"] - 0.5  # -0.5 .. +0.5
    multiplier = max(COMBINED_MIN_STAKE_MULT, min(COMBINED_MAX_STAKE_MULT, 1.0 + edge * 2.5))
    return round(FLAT_STAKE * multiplier, 4)


# Each strategy runs against the exact same incoming signals (this app's
# existing underdog/lock thresholds already applied upstream in
# confidence.py — a strategy here can only ADD a stricter filter on top,
# never loosen below what confidence.py already required, since anything
# looser was already discarded before reaching paper_trading at all).
# `stake_for(state, signal, row, signal_type)` — row/signal_type are only
# used by `combined`'s adaptive sizing; the flat-stake strategies ignore them.
STRATEGIES = {
    "underdogs": {
        "label": "Underdogs only — flat $5 stake",
        "filter_underdog": lambda s: True,
        "filter_lock": lambda s: False,
        "stake_for": _flat_stake,
    },
    "locks": {
        "label": "Locks only — flat $5 stake",
        "filter_underdog": lambda s: False,
        "filter_lock": lambda s: True,
        "stake_for": _flat_stake,
    },
    "combined": {
        "label": "Combined — underdogs + locks, stake learns from this account's own track record",
        "filter_underdog": lambda s: True,
        "filter_lock": lambda s: True,
        "stake_for": _combined_adaptive_stake,
    },
}


def _default_state():
    return {"starting_balance": STARTING_BALANCE, "balance": STARTING_BALANCE, "bets": []}


def _state_keys(strategy_id):
    local_key = f"paper_trades_{strategy_id}"
    return f"polymarket-bet-calculator:paper_trades:{strategy_id}", local_key


def _load_state(strategy_id):
    redis_key, local_key = _state_keys(strategy_id)
    return store.load(redis_key, local_key, _default_state())


def _save_state(state, strategy_id):
    redis_key, local_key = _state_keys(strategy_id)
    store.save(redis_key, local_key, state)


def _already_bet(state, event_id, outcome):
    return any(b["event_id"] == event_id and b["outcome"] == outcome for b in state["bets"])


def add_funds(amount, strategy_id):
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
        # What settle_pending_bets compares the real winning side against —
        # equal to `outcome` for every moneyline bet (team name) this app
        # places today, kept separate for forward-compatibility with any
        # future signal type whose outcome label and resolution key differ.
        "settlement_key": signal.get("settlement_key", signal["outcome"]),
        "token_id": signal.get("token_id"),
        "signal_type": signal_type,  # "underdog" | "lock"
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


def _run_strategy(strategy_id, cfg, underdog_rows, lock_rows):
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
        stake = cfg["stake_for"](state, signal, row, signal_type)
        bet = _place_one(state, row, signal, signal_type, stake)
        if bet:
            placed.append(bet)
            pending_by_league[row["league"]] += 1
        return False

    for row in _soonest_first(underdog_rows):
        signal = row.get("underdog_bet")
        if signal and cfg["filter_underdog"](signal) and _try_place(row, signal, "underdog"):
            break

    for row in _soonest_first(lock_rows):
        signal = row.get("lock")
        if signal and cfg["filter_lock"](signal) and _try_place(row, signal, "lock"):
            break

    if placed:
        _save_state(state, strategy_id)
    return placed


def run_all_strategies(underdog_rows, lock_rows):
    """Runs every registered strategy (STRATEGIES) against the same signal
    set — the actual "keep searching for a winning strategy" loop: each
    strategy accumulates its own real settlement history independently, so
    comparing them (get_strategy_comparison) over enough settled bets is
    what actually answers the question, not any single tuning pass."""
    placed = {}
    for strategy_id, cfg in STRATEGIES.items():
        placed[strategy_id] = _run_strategy(strategy_id, cfg, underdog_rows, lock_rows)
    return placed


def settle_pending_bets(strategy_id):
    state = _load_state(strategy_id)
    changed = False
    for bet in state["bets"]:
        if bet["status"] != "pending":
            continue
        try:
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


def get_summary(strategy_id):
    state = settle_pending_bets(strategy_id)  # always catch up settlements before reporting
    bets = state["bets"]
    settled = [b for b in bets if b["status"] != "pending"]
    pending = [b for b in bets if b["status"] == "pending"]
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

    comparison.sort(key=lambda c: (c["roi_pct"] is None, -(c["roi_pct"] or 0)))
    return comparison
