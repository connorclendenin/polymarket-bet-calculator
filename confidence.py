"""Combines Polymarket game data with the stats model (stats.py) to produce
a model confidence % per outcome, a live win-probability estimate for games
in progress, a "smart underdog bet" signal when the model's confidence in
the underdog sits well above its current market price, and a "lock" signal
for high-confidence favorites still paying a worthwhile multiplier.
"""

import matcher
import stats

UNDERDOG_EDGE_THRESHOLD = 0.07  # model confidence must beat market price by 7+ points

LOCK_MIN_CONFIDENCE = 0.80  # "super high confidence"
LOCK_MIN_PRICE = 0.55  # below this it's really an underdog play, not a lock
LOCK_MAX_PRICE = 0.90  # above this the payout is too thin to bother calling out

KELLY_FRACTION = 0.25  # quarter-Kelly — full Kelly is too aggressive given model uncertainty
MAX_STAKE_PCT_OF_BALANCE = 0.15  # never suggest more than 15% of buying power on one bet
MIN_MEANINGFUL_BALANCE = 0.50  # below this, a sized stake is just noise

NFL_PROP_EDGE_THRESHOLD = 0.08  # model probability must beat the line's price by 8+ points


def _resolve_home_away(outcomes):
    home = next((o for o in outcomes if o.get("home_away") == "home"), None)
    away = next((o for o in outcomes if o.get("home_away") == "away"), None)
    if home and away:
        return home, away
    # Fallback: Polymarket lists outcomes [away, home] in practice, but this
    # isn't guaranteed — flagged as a known limitation when metadata is missing.
    if len(outcomes) == 2:
        return outcomes[1], outcomes[0]
    return None, None


def attach_model_confidence(row, game):
    outcomes = game["outcomes"]
    home, away = _resolve_home_away(outcomes)
    if not home or not away:
        return

    home_out = next(o for o in row["outcomes"] if o["name"] == home["name"])
    away_out = next(o for o in row["outcomes"] if o["name"] == away["name"])

    if stats.is_supported(game["league"]):
        home_id = stats.find_espn_team_id(game["league"], home["name"])
        away_id = stats.find_espn_team_id(game["league"], away["name"])
        home_profile = stats.team_profile(game["league"], home_id) if home_id else None
        away_profile = stats.team_profile(game["league"], away_id) if away_id else None
        if home_profile and away_profile:
            home_prob, away_prob = stats.matchup_confidence(
                game["league"], home_profile, away_profile
            )
            home_out["model_prob"] = round(home_prob, 4)
            away_out["model_prob"] = round(away_prob, 4)
            home_out["profile"] = home_profile
            away_out["profile"] = away_profile
            row["model_basis"] = "full"
            return

    # Fallback: Polymarket's own season-record string, no ESPN data needed.
    home_prob, away_prob = stats.simple_confidence(home.get("record"), away.get("record"))
    if home_prob is None:
        row["model_basis"] = "insufficient_data"  # e.g. season hasn't started yet
        return
    home_out["model_prob"] = round(home_prob, 4)
    away_out["model_prob"] = round(away_prob, 4)
    row["model_basis"] = "basic"


def _find_live_score(league, home_name, away_name):
    if not stats.is_supported(league):
        return None
    home_id = stats.find_espn_team_id(league, home_name)
    away_id = stats.find_espn_team_id(league, away_name)
    if not home_id or not away_id:
        return None

    for g in stats.fetch_scoreboard(league):
        if g["state"] != "in":
            continue
        ids = set(g["team_scores"].keys())
        if {str(home_id), str(away_id)} <= ids:
            return {
                "home_score": g["team_scores"][str(home_id)]["score"],
                "away_score": g["team_scores"][str(away_id)]["score"],
                "period": g["period"],
                "detail": g["detail"],
                "espn_event_id": g["event_id"],
            }
    return None


def attach_live_estimate(row, game):
    if game.get("status") != "live":
        return

    outcomes = game["outcomes"]
    home, away = _resolve_home_away(outcomes)
    if not home or not away:
        return

    live = _find_live_score(game["league"], home["name"], away["name"])
    if not live:
        return

    home_out = next(o for o in row["outcomes"] if o["name"] == home["name"])
    away_out = next(o for o in row["outcomes"] if o["name"] == away["name"])

    home_pregame = home_out.get("model_prob", 0.5)
    away_pregame = away_out.get("model_prob", 0.5)

    home_live = stats.live_win_prob(
        game["league"], home_pregame, live["home_score"], live["away_score"], live["period"]
    )
    away_live = stats.live_win_prob(
        game["league"], away_pregame, live["away_score"], live["home_score"], live["period"]
    )
    # Normalize so the pair sums to 1 (each was computed independently).
    total = home_live + away_live
    if total > 0:
        home_live, away_live = home_live / total, away_live / total

    home_out["live_prob"] = round(home_live, 4)
    away_out["live_prob"] = round(away_live, 4)
    row["live_score"] = {
        "home": live["home_score"],
        "away": live["away_score"],
        "detail": live["detail"],
        "espn_event_id": live["espn_event_id"],
    }


def attach_underdog_bet(row):
    """Flags the single best 'smart underdog bet' on this game, if any: the
    underdog outcome (market price under 50%) whose model-estimated win
    probability — live if the game is in progress, pre-game otherwise —
    beats that market price by at least UNDERDOG_EDGE_THRESHOLD. Only one
    candidate per game, whichever has the bigger edge.
    """
    best = None
    for outcome in row["outcomes"]:
        if outcome.get("best_ask") is None or outcome["best_ask"] >= 0.5:
            continue  # only interested in the underdog side

        if "live_prob" in outcome:
            confidence_prob, basis = outcome["live_prob"], "live"
        elif "model_prob" in outcome and row.get("status") != "live":
            # Pre-game confidence is only meaningful before kickoff. A "live"
            # game with no live_prob means the live-score match failed (e.g.
            # ESPN already shows it final while Polymarket hasn't closed the
            # market yet) — comparing a stale pregame number against that
            # near-dead price produces nonsense edges, so skip it instead.
            confidence_prob, basis = outcome["model_prob"], "pregame"
        else:
            continue

        edge = round(confidence_prob - outcome["best_ask"], 4)
        if edge >= UNDERDOG_EDGE_THRESHOLD and (best is None or edge > best["edge"]):
            best = {
                "outcome": outcome["name"],
                "confidence": round(confidence_prob, 4),
                "market_price": outcome["best_ask"],
                "edge": edge,
                "basis": basis,
                "payout_multiplier": round(1 / outcome["best_ask"], 3)
                if outcome["best_ask"] > 0
                else None,
            }

    if best:
        row["underdog_bet"] = best


def attach_lock(row):
    """Flags the single best 'lock' on this game, if any: a favorite (market
    price LOCK_MIN_PRICE-LOCK_MAX_PRICE, i.e. still paying a real multiplier,
    not already bid up near $1) that the model is independently very
    confident in (>= LOCK_MIN_CONFIDENCE). Unlike the underdog signal, this
    isn't about the model disagreeing with the market — it's model and
    market agreeing a team is a strong favorite, while the price still
    leaves a worthwhile payout. Live confidence is used when available,
    pre-game otherwise (same live/pregame gating as attach_underdog_bet).
    """
    best = None
    for outcome in row["outcomes"]:
        price = outcome.get("best_ask")
        if price is None or not (LOCK_MIN_PRICE <= price <= LOCK_MAX_PRICE):
            continue

        if "live_prob" in outcome:
            confidence_prob, basis = outcome["live_prob"], "live"
        elif "model_prob" in outcome and row.get("status") != "live":
            confidence_prob, basis = outcome["model_prob"], "pregame"
        else:
            continue

        if confidence_prob < LOCK_MIN_CONFIDENCE:
            continue
        if best is None or confidence_prob > best["confidence"]:
            best = {
                "outcome": outcome["name"],
                "confidence": round(confidence_prob, 4),
                "market_price": price,
                "payout_multiplier": round(1 / price, 3),
                "basis": basis,
            }

    if best:
        row["lock"] = best


def _kelly_fraction(confidence_prob, price):
    """Quarter-Kelly stake sizing. b = net odds (payout per $1 staked, minus
    the stake itself); full formula is f* = p - (1-p)/b, clamped at 0."""
    if price is None or not (0 < price < 1):
        return 0.0
    b = (1.0 / price) - 1.0
    if b <= 0:
        return 0.0
    f = confidence_prob - (1.0 - confidence_prob) / b
    return max(0.0, f) * KELLY_FRACTION


def attach_bet_sizing(row, buying_power):
    """Adds a suggested stake to row['underdog_bet'] and/or row['lock'],
    sized off the user's real polymarket.us buying power via quarter-Kelly,
    capped at MAX_STAKE_PCT_OF_BALANCE. Does nothing if buying_power is
    unavailable (no account connected) or too small to size meaningfully.
    """
    if buying_power is None:
        return
    if buying_power < MIN_MEANINGFUL_BALANCE:
        row["balance_too_low"] = True
        return

    for key in ("underdog_bet", "lock"):
        signal = row.get(key)
        if not signal:
            continue
        fraction = min(
            _kelly_fraction(signal["confidence"], signal["market_price"]),
            MAX_STAKE_PCT_OF_BALANCE,
        )
        signal["suggested_stake"] = round(fraction * buying_power, 4)
        signal["suggested_stake_pct"] = round(fraction, 4)


def _slug_mentions_both_teams(slug, team_names):
    if not slug or not team_names:
        return False
    slug_lower = slug.lower()
    hits = 0
    for name in team_names:
        token = name.split()[-1].lower()  # last word, e.g. "Patriots" from "New England Patriots"
        if token and token in slug_lower:
            hits += 1
    return hits >= 2


def attach_your_position(row, game, us_positions):
    """Flags when the user already holds a position in this game on their
    polymarket.us account, matched by team-name overlap with the position's
    market slug (the two Polymarket products don't share market ids, so
    there's no exact key to join on). Marks whether it conflicts with this
    row's underdog/lock signal when an outcome can be determined — the
    position schema isn't fully confirmed yet (the account had no open
    positions to inspect when this was built), so this degrades gracefully
    if expected fields are missing rather than guessing wrong.
    """
    if not us_positions:
        return

    for slug, position in us_positions.items():
        if not _slug_mentions_both_teams(slug, game.get("team_names", [])):
            continue

        outcome = position.get("outcome") or position.get("side")
        info = {
            "market_slug": slug,
            "outcome": outcome,
            "size": position.get("size"),
        }

        signal = row.get("underdog_bet") or row.get("lock")
        if outcome and signal:
            # `outcome` is the .us account's own full-name string ("San
            # Francisco 49ers"); `signal["outcome"]` is Polymarket's short
            # name ("49ers") — an exact-string compare here would read as
            # "conflicts" even when they're the same team held on the same
            # side. Caught and fixed alongside the live position tracker,
            # which hit the identical short-vs-full mismatch.
            info["conflicts"] = not matcher._team_in_name(signal["outcome"], outcome)

        row["your_position"] = info
        return


def attach_position_tracker(row):
    """For a live game where attach_your_position already found a real held
    position, attaches a per-play confidence feed via
    stats.fetch_live_play_feed — ESPN's own real win-probability series, so
    the number genuinely moves with every tracked play instead of only
    when the score itself changes between polls. Requires row['live_score']
    (set by attach_live_estimate) for the ESPN event id; does nothing if
    that's missing, the game isn't live, no position is held, or this
    league/game doesn't expose ESPN's per-play series (confirmed: NHL
    never does — see fetch_live_play_feed)."""
    if row.get("status") != "live" or not row.get("your_position"):
        return
    espn_event_id = (row.get("live_score") or {}).get("espn_event_id")
    if not espn_event_id:
        return

    feed = stats.fetch_live_play_feed(row["league"], espn_event_id)
    if not feed:
        return

    home_out = next((o for o in row["outcomes"] if o.get("home_away") == "home"), None)
    away_out = next((o for o in row["outcomes"] if o.get("home_away") == "away"), None)
    if not home_out or not away_out:
        return

    # Polymarket's own outcome names are short ("49ers"); the .us position's
    # outcome field is the account's own full-name string ("San Francisco
    # 49ers") — same short-vs-full mismatch matcher.py already handles for
    # sportsbook-odds matching, so reuse it rather than requiring an exact
    # match that would never actually hit.
    position_outcome = row["your_position"].get("outcome") or ""
    tracks_home = matcher._team_in_name(home_out["name"], position_outcome)
    tracks_away = matcher._team_in_name(away_out["name"], position_outcome)

    timeline = []
    for p in feed:
        home_pct = p["home_win_pct"]
        your_pct = home_pct if tracks_home else round(1 - home_pct, 4) if tracks_away else None
        timeline.append(
            {
                "text": p["text"],
                "period": p["period"],
                "clock": p.get("clock"),
                "scoring_play": p["scoring_play"],
                "home_win_pct": home_pct,
                "your_win_pct": your_pct,
            }
        )

    row["position_tracker"] = {
        # None when the position's own outcome string couldn't be matched to
        # either team name — the "which side is mine" gap already flagged as
        # unverified on attach_your_position. The feed and home-side
        # probability still show in that case; only `your_win_pct` is blank.
        "matched_side": "home" if tracks_home else "away" if tracks_away else None,
        "home_team": home_out["name"],
        "away_team": away_out["name"],
        "timeline": timeline,
    }


def _closest_to_even(lines):
    """The line whose Gamma snapshot price sits nearest 50/50 — used only to
    pick WHICH line to evaluate (the real-money analogue of 'the total a
    sportsbook would actually post', out of Polymarket's many parallel O/U
    lines for the same market). The actual edge is computed later from the
    live order book, not this snapshot price, since far-out/untraded lines
    sit at a meaningless ~50/50 default (confirmed empirically: an untraded
    line shows bestBid=None and a 0.99-wide crossed spread) that would
    otherwise look like a huge, fake edge.
    """
    return min(lines, key=lambda l: abs(l["over_price"] - 0.5)) if lines else None


def select_nfl_prop_candidates(game):
    """Picks the one game-total line and one team-total line per team worth
    evaluating for this game, and returns their token ids so the caller can
    fetch real order-book prices for them in the same batched CLOB call as
    everything else — see attach_nfl_props for why a live price is required
    rather than trusting Gamma's snapshot."""
    if game.get("league") != "nfl":
        return []

    candidates = []
    total_line = _closest_to_even(game.get("totals") or [])
    if total_line:
        candidates.append({"market": "Game total", "team": None, "stdev": "game", "line": total_line})

    team_totals = game.get("team_totals") or {}
    for outcome in game.get("outcomes", []):
        team_line = _closest_to_even(team_totals.get(outcome["name"]) or [])
        if team_line:
            candidates.append(
                {
                    "market": f"{outcome['name']} total",
                    "team": outcome["name"],
                    "stdev": "team",
                    "line": team_line,
                }
            )
    return candidates


def _evaluate_prop_candidate(candidate, projected, books):
    line = candidate["line"]
    over_book = books.get(line["over_token_id"]) or {}
    under_book = books.get(line["under_token_id"]) or {}
    over_ask = over_book.get("best_ask")
    under_ask = under_book.get("best_ask")
    if over_ask is None and under_ask is None:
        return None  # no real two-sided market on this line — skip rather than fake it

    stdev = stats.GAME_TOTAL_STDEV if candidate["stdev"] == "game" else stats.TEAM_TOTAL_STDEV
    model_over = stats.prob_over(line["line"], projected, stdev)

    best = None
    if over_ask is not None:
        edge = round(model_over - over_ask, 4)
        if edge >= NFL_PROP_EDGE_THRESHOLD:
            best = ("Over", over_ask, model_over, edge, line["over_token_id"])
    if under_ask is not None:
        edge = round((1 - model_over) - under_ask, 4)
        if edge >= NFL_PROP_EDGE_THRESHOLD and (best is None or edge > best[3]):
            best = ("Under", under_ask, 1 - model_over, edge, line["under_token_id"])

    if not best:
        return None
    side, price, model_prob, edge, token_id = best
    return {
        "market": candidate["market"],
        "team": candidate["team"],
        "side": side,
        "line": line["line"],
        "market_price": price,
        "model_prob": round(model_prob, 4),
        "projected": round(projected, 1),
        "edge": edge,
        "token_id": token_id,
    }


def attach_nfl_props(row, game, candidates, books):
    """Smart NFL prop bets — game total and each team's total points,
    projected from the same ESPN scoring data the main confidence model
    already pulled for this game (no extra fetches beyond the order-book
    lookup). Polymarket doesn't offer individual player props at all right
    now (checked directly against the live market data before building
    this — every NFL market beyond moneyline is a team/game-level
    derivative: totals, team totals, spreads, by quarter/half), so this
    covers totals/team-totals, the ones this app's existing team-scoring
    stats can actually back up. `candidates` comes from
    select_nfl_prop_candidates (run before the batched book fetch);
    `books` is the same best-bid/ask map used for every other signal.
    """
    if not candidates or row.get("model_basis") != "full":
        return

    outcomes = row["outcomes"]
    home_out = next((o for o in outcomes if o.get("home_away") == "home"), None)
    away_out = next((o for o in outcomes if o.get("home_away") == "away"), None)
    if not home_out or not away_out:
        return

    home_profile = home_out.get("profile")
    away_profile = away_out.get("profile")
    if not home_profile or not away_profile:
        return

    evaluated = []
    for candidate in candidates:
        if candidate["team"] is None:
            projected = stats.project_game_total(home_profile, away_profile)
        elif candidate["team"] == home_out["name"]:
            projected = stats.project_team_points(home_profile, away_profile)
        elif candidate["team"] == away_out["name"]:
            projected = stats.project_team_points(away_profile, home_profile)
        else:
            continue
        result = _evaluate_prop_candidate(candidate, projected, books)
        if result:
            evaluated.append(result)

    if evaluated:
        evaluated.sort(key=lambda c: c["edge"], reverse=True)
        row["prop_bet"] = evaluated[0]
