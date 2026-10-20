"""Stats-based confidence model.

Builds a win-probability estimate per team from real season data (record,
home/road splits, recent form, scoring margin) pulled from ESPN's public
scoreboard/schedule endpoints (keyless). This is a transparent heuristic
model, not a trained machine-learning model — the formulas are documented
inline so the numbers are explainable, not a black box.

Covers the four leagues with small, stable rosters where ESPN team ids can
be reliably mapped (NFL, NBA, MLB, NHL). CFB/CBB have hundreds of teams and
aren't mapped here, so those leagues fall back to Polymarket's own
season-record field only (see `simple_confidence`).
"""

import math
import re
from datetime import datetime, timezone

import requests

from cache import TTLCache

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

ESPN_SPORT_PATH = {
    "nfl": "football/nfl",
    "nba": "basketball/nba",
    "mlb": "baseball/mlb",
    "nhl": "hockey/nhl",
}

# Periods per regulation game, used to approximate how much of the game is
# left. Overtime periods just get clamped to "nearly done" rather than
# modeled exactly.
TOTAL_PERIODS = {"nfl": 4, "nba": 4, "mlb": 9, "nhl": 3}

# How many points/goals/runs typically separate a near-even game from a
# comfortable one, per league. Tunable heuristic, not fit to historical data.
MARGIN_SCALE = {"nfl": 7.0, "nba": 8.0, "mlb": 1.5, "nhl": 1.3}

_cache = TTLCache()
_TEAMS_TTL = 86400
_SCHEDULE_TTL = 3600
_SCOREBOARD_TTL = 20
_FAIL_TTL = 30  # ESPN's unofficial API is occasionally slow/unresponsive; don't
# retry the same team repeatedly within one scan when it just failed — a short
# negative cache lets the scan move on fast while still recovering quickly
# once ESPN is responsive again.


def _get(url, params=None, timeout=6):
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _normalize(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def is_supported(league):
    return league in ESPN_SPORT_PATH


def fetch_espn_teams(league):
    cache_key = f"espn_teams:{league}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        path = ESPN_SPORT_PATH[league]
        data = _get(f"{ESPN_BASE}/{path}/teams", params={"limit": 60})
        teams = [
            {
                "id": t["team"]["id"],
                "abbreviation": t["team"].get("abbreviation", ""),
                "display_name": t["team"].get("displayName", ""),
                "short_name": t["team"].get("shortDisplayName", ""),
            }
            for t in data["sports"][0]["leagues"][0]["teams"]
        ]
    except requests.exceptions.RequestException:
        _cache.set(cache_key, [], _FAIL_TTL)
        return []

    _cache.set(cache_key, teams, _TEAMS_TTL)
    return teams


def find_espn_team_id(league, polymarket_name):
    """Match a Polymarket team name/alias to an ESPN team id by abbreviation
    or name overlap. Returns None if no confident match is found."""
    teams = fetch_espn_teams(league)
    target = _normalize(polymarket_name)
    if not target:
        return None

    for t in teams:
        if _normalize(t["abbreviation"]) == target:
            return t["id"]

    for t in teams:
        norm_display = _normalize(t["display_name"])
        norm_short = _normalize(t["short_name"])
        if target in norm_display or norm_display in target or target == norm_short:
            return t["id"]

    return None


def fetch_team_schedule(league, espn_team_id):
    cache_key = f"espn_schedule:{league}:{espn_team_id}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        path = ESPN_SPORT_PATH[league]
        data = _get(f"{ESPN_BASE}/{path}/teams/{espn_team_id}/schedule")
    except requests.exceptions.RequestException:
        _cache.set(cache_key, [], _FAIL_TTL)
        return []

    games = []
    for event in data.get("events", []):
        comp = (event.get("competitions") or [{}])[0]
        state = comp.get("status", {}).get("type", {}).get("state")
        if state != "post":
            continue  # only completed games count toward form
        competitors = comp.get("competitors", [])
        me = next((c for c in competitors if c.get("team", {}).get("id") == str(espn_team_id)), None)
        opp = next((c for c in competitors if c.get("team", {}).get("id") != str(espn_team_id)), None)
        if not me or not opp:
            continue
        my_score = (me.get("score") or {}).get("value")
        opp_score = (opp.get("score") or {}).get("value")
        if my_score is None or opp_score is None:
            continue
        games.append(
            {
                "date": event.get("date"),
                "home_away": me.get("homeAway"),
                "won": bool(me.get("winner")),
                "points_for": my_score,
                "points_against": opp_score,
            }
        )
    games.sort(key=lambda g: g["date"])
    _cache.set(cache_key, games, _SCHEDULE_TTL)
    return games


def fetch_scoreboard(league):
    cache_key = f"espn_scoreboard:{league}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        path = ESPN_SPORT_PATH[league]
        data = _get(f"{ESPN_BASE}/{path}/scoreboard")
    except requests.exceptions.RequestException:
        _cache.set(cache_key, [], _FAIL_TTL)
        return []

    games = []
    for event in data.get("events", []):
        comp = (event.get("competitions") or [{}])[0]
        status = event.get("status", {})
        state = status.get("type", {}).get("state")
        competitors = comp.get("competitors", [])
        team_scores = {
            c.get("team", {}).get("id"): {
                "score": int(c["score"]) if c.get("score") not in (None, "") else None,
                "home_away": c.get("homeAway"),
                "abbreviation": c.get("team", {}).get("abbreviation"),
            }
            for c in competitors
        }
        games.append(
            {
                "event_id": event.get("id"),
                "state": state,  # "pre" | "in" | "post"
                "period": status.get("period"),
                "display_clock": status.get("displayClock"),
                "detail": status.get("type", {}).get("shortDetail"),
                "team_scores": team_scores,
            }
        )
    _cache.set(cache_key, games, _SCOREBOARD_TTL)
    return games


def _win_pct(games):
    if not games:
        return None
    return sum(1 for g in games if g["won"]) / len(games)


def team_profile(league, espn_team_id):
    """Season record, home/road split, last-10 form, and scoring margin."""
    games = fetch_team_schedule(league, espn_team_id)
    if not games:
        return None

    home_games = [g for g in games if g["home_away"] == "home"]
    away_games = [g for g in games if g["home_away"] == "away"]
    last10 = games[-10:]

    avg_for = sum(g["points_for"] for g in last10) / len(last10)
    avg_against = sum(g["points_against"] for g in last10) / len(last10)

    return {
        "games_played": len(games),
        "season_win_pct": _win_pct(games),
        "home_win_pct": _win_pct(home_games),
        "away_win_pct": _win_pct(away_games),
        "last10_win_pct": _win_pct(last10),
        "avg_points_for": round(avg_for, 1),
        "avg_points_against": round(avg_against, 1),
        "avg_margin": round(avg_for - avg_against, 1),
    }


def _log5(p_a, p_b):
    """Bill James' log5 formula: win probability for A over B given each
    team's context win percentage against a league-average opponent."""
    denom = p_a + p_b - 2 * p_a * p_b
    if denom <= 0:
        return 0.5
    return (p_a - p_a * p_b) / denom


def _context_win_pct(profile, is_home):
    split = profile["home_win_pct"] if is_home else profile["away_win_pct"]
    components = [(profile["season_win_pct"], 0.5)]
    if split is not None:
        components.append((split, 0.3))
    if profile["last10_win_pct"] is not None:
        components.append((profile["last10_win_pct"], 0.2))
    total_weight = sum(w for _, w in components)
    blended = sum(p * w for p, w in components) / total_weight
    return min(max(blended, 0.03), 0.97)  # clamp away from 0/1 so log5 stays finite


def matchup_confidence(league, home_profile, away_profile):
    """Stats-based win probability for (home, away), independent of
    Polymarket's price — built purely from each team's own performance."""
    p_home = _context_win_pct(home_profile, is_home=True)
    p_away = _context_win_pct(away_profile, is_home=False)
    log5_home = _log5(p_home, p_away)

    scale = MARGIN_SCALE.get(league, 5.0)
    margin_diff = home_profile["avg_margin"] - away_profile["avg_margin"]
    margin_prob_home = 1.0 / (1.0 + math.exp(-margin_diff / scale))

    combined_home = 0.6 * log5_home + 0.4 * margin_prob_home
    combined_home = min(max(combined_home, 0.02), 0.98)
    return combined_home, 1 - combined_home


def simple_confidence(home_record, away_record):
    """Fallback for leagues without an ESPN mapping (CFB/CBB), or when the
    full model has no usable data: log5 off Polymarket's own season W-L
    record string alone, e.g. '5-2-0'. Returns (None, None) when NEITHER
    team has a usable record (e.g. season hasn't started) rather than
    quietly defaulting to an uninformative 50/50 that would look like a
    real signal."""

    def parse_pct(record):
        if not record:
            return None
        parts = re.findall(r"\d+", record)
        if len(parts) < 2:
            return None
        wins, losses = int(parts[0]), int(parts[1])
        ties = int(parts[2]) if len(parts) > 2 else 0
        total = wins + losses + ties
        if total == 0:
            return None
        return min(max((wins + 0.5 * ties) / total, 0.03), 0.97)

    p_home = parse_pct(home_record)
    p_away = parse_pct(away_record)
    if p_home is None and p_away is None:
        return None, None
    p_home = p_home if p_home is not None else 0.5
    p_away = p_away if p_away is not None else 0.5
    home_prob = _log5(p_home, p_away)
    return home_prob, 1 - home_prob


def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def _logit(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


_PLAY_FEED_TTL = 15  # genuinely live tracking wants fresher data than the 20s scoreboard poll


def fetch_live_play_feed(league, espn_event_id):
    """Per-play win-probability feed for one live game, using ESPN's own
    real win-probability series (`summary`'s `winprobability` field, keyed
    by play id) merged with that play's text/period/clock — not this app's
    own score-based heuristic, since that only changes when polled after a
    score update, while this genuinely varies with every play ESPN tracks
    one. Confirmed empirically: NFL and MLB both expose this; NHL's summary
    endpoint returns `winprobability: null` (no per-play model), so this
    returns [] there rather than fabricate per-play granularity that
    doesn't exist — the existing score/period-based live_win_prob is still
    the live estimate shown in that case. Returns entries in chronological
    order, oldest first."""
    cache_key = f"espn_playfeed:{league}:{espn_event_id}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        path = ESPN_SPORT_PATH[league]
        data = _get(f"{ESPN_BASE}/{path}/summary", params={"event": espn_event_id}, timeout=8)
    except requests.exceptions.RequestException:
        _cache.set(cache_key, [], _FAIL_TTL)
        return []

    win_prob = data.get("winprobability") or []
    if not win_prob:
        _cache.set(cache_key, [], _FAIL_TTL)
        return []

    plays_by_id = {}
    drives = data.get("drives")
    if drives:  # football: plays nested under drives, not a flat top-level list
        for drive in drives.get("previous") or []:
            for p in drive.get("plays") or []:
                plays_by_id[p["id"]] = p
        for p in (drives.get("current") or {}).get("plays") or []:
            plays_by_id[p["id"]] = p
    else:  # baseball/other sports: already a flat top-level list
        for p in data.get("plays") or []:
            plays_by_id[p["id"]] = p

    feed = []
    for entry in win_prob:
        play = plays_by_id.get(entry.get("playId"))
        if not play or not play.get("text"):
            continue  # most plays carry a winprobability entry; skip any we can't label
        period = play.get("period") or {}
        feed.append(
            {
                "play_id": entry["playId"],
                "text": play["text"],
                "period": period.get("displayValue") or period.get("number"),
                "clock": (play.get("clock") or {}).get("displayValue"),
                "scoring_play": bool(play.get("scoringPlay")),
                "home_win_pct": round(entry["homeWinPercentage"], 4),
            }
        )

    _cache.set(cache_key, feed, _PLAY_FEED_TTL)
    return feed


def live_win_prob(league, pregame_prob, score_for, score_against, period):
    """Heuristic live win-probability estimate: blends the pregame model
    with the current score, weighting the score more heavily as the game
    progresses. This is an approximation, not a trained in-game model —
    treat it as a rough signal, not a precise probability."""
    total_periods = TOTAL_PERIODS.get(league, 4)
    period = period or 1
    elapsed = min(0.97, max(0.0, (period - 1 + 0.5) / total_periods))

    scale = MARGIN_SCALE.get(league, 5.0)
    score_diff = (score_for or 0) - (score_against or 0)
    elapsed_weight = 0.5 + 1.5 * elapsed  # same score matters more as the clock runs down
    score_logit = (score_diff / scale) * elapsed_weight

    pregame_logit = _logit(pregame_prob)
    combined_logit = pregame_logit * (1 - elapsed) + score_logit
    return _sigmoid(combined_logit)


# --- Game/team totals (O/U) projection ---
#
# Standard sports-betting projection technique: a team's expected points is
# the average of (how many points it usually scores) and (how many points
# its opponent usually allows). Summing both teams' expected points gives
# the game total. Turning "projected total vs. a specific O/U line" into a
# probability uses a Normal approximation around that projection — the
# standard deviations below are reasonable, commonly-cited figures for NFL
# scoring variance, not fit to this app's own data, so treat the resulting
# probabilities as a heuristic estimate, same spirit as the live win-prob
# model above.
GAME_TOTAL_STDEV = 13.5
TEAM_TOTAL_STDEV = 10.0


def project_team_points(team_profile, opponent_profile):
    return (team_profile["avg_points_for"] + opponent_profile["avg_points_against"]) / 2


def project_game_total(home_profile, away_profile):
    home_pts = project_team_points(home_profile, away_profile)
    away_pts = project_team_points(away_profile, home_profile)
    return home_pts + away_pts


def _normal_cdf(x, mean, stdev):
    if stdev <= 0:
        return 1.0 if x >= mean else 0.0
    z = (x - mean) / (stdev * math.sqrt(2))
    return 0.5 * (1 + math.erf(z))


def prob_over(line, projected_mean, stdev):
    """P(actual total > line) under a Normal(projected_mean, stdev) model."""
    return 1 - _normal_cdf(line, projected_mean, stdev)
