import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

import confidence
import matcher
import oddsapi
import paper_trading
import performance
import polymarket
import store
import us_api

app = Flask(__name__)

DEFAULT_MIN_ARB_EDGE = 0.01  # 1 cent of guaranteed profit per $1 of combined outcome cost
DEFAULT_MIN_VALUE_EDGE = 0.04  # 4 points of probability gap vs. sportsbook consensus

# The full scan is expensive (hundreds of games across Gamma/CLOB/ESPN, ~50s
# cold) — fine for local dev's 30s poll against a warm in-process cache, but
# would time out a Vercel serverless function recomputing it on every page
# load with a cold cache every time. When deployed (store.is_configured()),
# the scheduled job (paper_trade_cron.py) does this expensive work on its
# own schedule and caches the result here; /api/scan and /api/paper then
# just read that cache instead of recomputing — see cache_scan_snapshot and
# its read side in api_scan/api_paper below.
_SCAN_CACHE_REDIS_KEY = "polymarket-bet-calculator:latest_scan"
_SCAN_CACHE_LOCAL_KEY = "latest_scan"


def cache_scan_snapshot(rows):
    """Called by paper_trade_cron.py after a full scan — not used by the
    live app itself, which only ever READS this (see api_scan)."""
    store.save(
        _SCAN_CACHE_REDIS_KEY,
        _SCAN_CACHE_LOCAL_KEY,
        {"games": rows, "computed_at": datetime.now(timezone.utc).isoformat()},
    )


def _build_game_row(game, books):
    outcomes_out = []
    ask_sum = 0.0
    all_priced = True

    for outcome in game["outcomes"]:
        book = books.get(outcome["token_id"]) or {}
        best_bid = book.get("best_bid")
        best_ask = book.get("best_ask")
        if best_ask is None:
            all_priced = False
        else:
            ask_sum += best_ask
        outcomes_out.append(
            {
                "name": outcome["name"],
                "best_bid": best_bid,
                "best_ask": best_ask,
                "logo": outcome.get("logo"),
                "color": outcome.get("color"),
                "home_away": outcome.get("home_away"),
            }
        )

    arb = None
    if all_priced and outcomes_out:
        edge = round(1.0 - ask_sum, 4)
        if edge >= DEFAULT_MIN_ARB_EDGE:
            stake = round(ask_sum, 4)
            arb = {
                "cost_to_buy_all_outcomes": stake,
                "guaranteed_payout": 1.0,
                "edge_dollars": edge,
                "roi_pct": round((edge / stake) * 100, 2) if stake > 0 else None,
            }

    row = {
        "event_id": game["event_id"],
        "league": game["league"],
        "title": game["title"],
        "polymarket_url": f"https://polymarket.com/event/{game['slug']}",
        "game_start": game["game_start"],
        "status": game.get("status", "upcoming"),
        "outcomes": outcomes_out,
        "arbitrage": arb,
        "value_bets": [],
        "model_basis": None,
    }
    return row


def _attach_value_bets(row, game, odds_events):
    if odds_events is None:
        return

    odds_event = matcher.find_odds_event(game["team_names"], game["game_start"], odds_events)
    if not odds_event:
        return

    consensus = odds_event["consensus_prob"]
    row["sportsbook_match"] = {
        "home_team": odds_event["home_team"],
        "away_team": odds_event["away_team"],
        "book_count": odds_event["book_count"],
    }

    for outcome in row["outcomes"]:
        if outcome["best_ask"] is None:
            continue
        sportsbook_prob = next(
            (
                prob
                for team, prob in consensus.items()
                if matcher._team_in_name(outcome["name"], team)
            ),
            None,
        )
        if sportsbook_prob is None:
            continue

        edge = round(sportsbook_prob - outcome["best_ask"], 4)
        outcome["sportsbook_implied_prob"] = round(sportsbook_prob, 4)
        if edge >= DEFAULT_MIN_VALUE_EDGE:
            row["value_bets"].append(
                {
                    "outcome": outcome["name"],
                    "polymarket_ask": outcome["best_ask"],
                    "sportsbook_prob": round(sportsbook_prob, 4),
                    "edge": edge,
                }
            )


@app.route("/")
def index():
    return render_template(
        "index.html",
        odds_configured=oddsapi.is_configured(),
        leagues=list(polymarket.LEAGUE_TAGS.keys()),
    )


def _run_scan(leagues):
    """Builds scored game rows (arb/value/confidence/underdog/lock) for the
    given leagues (None = all). Shared by /api/scan (whatever the user has
    selected) and the paper trader (always all leagues, independent of what
    the UI happens to be filtered to)."""
    games = polymarket.fetch_games(leagues)
    all_token_ids = [o["token_id"] for g in games for o in g["outcomes"]]

    # NFL prop candidates need a real order-book price, not Gamma's often-stale
    # snapshot (an untraded line sits at a meaningless ~50/50 default) — collect
    # their tokens now so they ride along in this same batched CLOB call.
    prop_candidates_by_event = {}
    for game in games:
        candidates = confidence.select_nfl_prop_candidates(game)
        if candidates:
            prop_candidates_by_event[game["event_id"]] = candidates
            for c in candidates:
                all_token_ids.append(c["line"]["over_token_id"])
                all_token_ids.append(c["line"]["under_token_id"])

    books = polymarket.fetch_books(all_token_ids)

    odds_by_league = {}
    if oddsapi.is_configured():
        for league in set(g["league"] for g in games):
            try:
                odds_by_league[league] = oddsapi.fetch_odds(league)
            except Exception:
                odds_by_league[league] = []

    buying_power = None
    us_positions = {}
    if us_api.is_configured() and not us_api.dependency_missing():
        try:
            buying_power = us_api.fetch_buying_power()
            us_positions = us_api.fetch_positions()
        except Exception:
            pass  # account data is best-effort; scan still works without it

    rows = []
    for game in games:
        row = _build_game_row(game, books)
        _attach_value_bets(row, game, odds_by_league.get(game["league"]))
        try:
            confidence.attach_model_confidence(row, game)
            confidence.attach_live_estimate(row, game)
            confidence.attach_underdog_bet(row)
            confidence.attach_lock(row)
            confidence.attach_bet_sizing(row, buying_power)
            confidence.attach_your_position(row, game, us_positions)
            confidence.attach_position_tracker(row)
            confidence.attach_nfl_props(
                row, game, prop_candidates_by_event.get(game["event_id"], []), books
            )
        except Exception:
            pass  # stats model is best-effort; never let it break the scan
        rows.append(row)
    return rows


def _ranked_signal_lists(rows):
    arb_rows = [r for r in rows if r["arbitrage"]]
    value_rows = [r for r in rows if r["value_bets"]]
    underdog_rows = [r for r in rows if r.get("underdog_bet")]
    lock_rows = [r for r in rows if r.get("lock")]
    prop_rows = [r for r in rows if r.get("prop_bet")]
    arb_rows.sort(key=lambda r: r["arbitrage"]["edge_dollars"], reverse=True)
    value_rows.sort(key=lambda r: max(b["edge"] for b in r["value_bets"]), reverse=True)
    underdog_rows.sort(key=lambda r: r["underdog_bet"]["edge"], reverse=True)
    lock_rows.sort(key=lambda r: r["lock"]["confidence"], reverse=True)
    prop_rows.sort(key=lambda r: r["prop_bet"]["edge"], reverse=True)
    return arb_rows, value_rows, underdog_rows, lock_rows, prop_rows


@app.route("/api/scan")
def api_scan():
    leagues_param = request.args.get("leagues")
    leagues = leagues_param.split(",") if leagues_param else None
    scan_computed_at = None

    if store.is_configured():
        # Deployed: never recompute live (would time out) — read the
        # scheduled job's last cached scan instead.
        cached = store.load(_SCAN_CACHE_REDIS_KEY, _SCAN_CACHE_LOCAL_KEY, None)
        if cached is None:
            return jsonify(
                {"error": "no scan cached yet — the scheduled job hasn't run", "games": []}
            ), 503
        rows = cached["games"]
        scan_computed_at = cached.get("computed_at")
        if leagues:
            rows = [r for r in rows if r["league"] in leagues]
    else:
        try:
            rows = _run_scan(leagues)
        except Exception as exc:
            # Per-league/per-chunk failures are already absorbed inside
            # _run_scan; this is a last-resort net for anything unexpected
            # so the frontend gets a clean error to retry on instead of a
            # 500 and a blank screen.
            return jsonify({"error": f"scan failed: {exc}", "games": []}), 502

    arb_rows, value_rows, underdog_rows, lock_rows, prop_rows = _ranked_signal_lists(rows)

    response = {
        "games": rows,
        "arbitrage_opportunities": arb_rows,
        "value_opportunities": value_rows,
        "underdog_opportunities": underdog_rows,
        "lock_opportunities": lock_rows,
        "prop_opportunities": prop_rows,
        "odds_configured": oddsapi.is_configured(),
        "game_count": len(rows),
    }
    if scan_computed_at:
        # Deployed data is only as fresh as the scheduled job's last run
        # (hourly) — the frontend shows this instead of "just now" so it
        # doesn't look more live than it actually is.
        response["scan_computed_at"] = scan_computed_at
    return jsonify(response)


@app.route("/api/paper")
def api_paper():
    if not store.is_configured():
        # Local dev: place/settle live on every poll, same as always.
        try:
            rows = _run_scan(None)  # always all leagues, regardless of UI filter
            _, _, underdog_rows, lock_rows, prop_rows = _ranked_signal_lists(rows)
            paper_trading.run_all_strategies(underdog_rows, lock_rows, prop_rows)
        except Exception:
            pass  # placing new bets is best-effort; still report current state below
    # Deployed: the scheduled job already placed/settled bets on its own
    # schedule — just report current state, which is fast (reads Redis, no
    # live scan) and safely within a serverless function's time limit.

    summary = paper_trading.get_summary("baseline")
    summary["strategy_comparison"] = paper_trading.get_strategy_comparison()
    return jsonify(summary)


@app.route("/api/us-account")
def api_us_account():
    if not us_api.is_configured():
        return jsonify({"configured": False})

    if us_api.dependency_missing():
        return jsonify(
            {
                "configured": True,
                "error": "The 'cryptography' package isn't installed yet — run "
                "`.venv/bin/pip install cryptography` in the project directory, "
                "then reload.",
            }
        )

    try:
        summary = us_api.get_account_summary()
    except Exception as exc:
        return jsonify({"configured": True, "error": f"API request failed: {exc}"}), 502

    capital_history = None
    try:
        buying_power = us_api.fetch_buying_power()
        usd_balance = next(
            (b.get("currentBalance") for b in summary["balances"] if b.get("currency") == "USD"),
            buying_power,
        )
        activities = us_api.fetch_activities()
        capital_history = performance.build_us_capital_series(activities, usd_balance)
    except Exception:
        pass  # capital-flow chart is best-effort; balances/positions still return without it

    return jsonify({"configured": True, "capital_history": capital_history, **summary})


@app.route("/api/positions")
def api_positions():
    address = request.args.get("address", "").strip()
    if not address:
        return jsonify({"error": "missing wallet address"}), 400

    try:
        positions = polymarket.fetch_positions(address)
        value = polymarket.fetch_portfolio_value(address)
    except Exception as exc:
        return jsonify({"error": f"couldn't look up that address: {exc}"}), 502

    open_positions = [p for p in positions if not p.get("redeemable")]

    pnl_history = []
    try:
        activity = polymarket.fetch_activity(address)
        pnl_history = performance.build_pnl_series(activity)
    except Exception:
        pass  # P/L history is best-effort; positions still return without it

    return jsonify(
        {
            "positions": open_positions,
            "portfolio_value": value,
            "pnl_history": pnl_history,
        }
    )


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "odds_configured": oddsapi.is_configured()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5007))
    app.run(host="127.0.0.1", port=port, debug=True)
