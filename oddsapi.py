import os

import requests

from cache import TTLCache

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

SPORT_KEY_MAP = {
    "nfl": "americanfootball_nfl",
    "nba": "basketball_nba",
    "mlb": "baseball_mlb",
    "nhl": "icehockey_nhl",
    "cfb": "americanfootball_ncaaf",
    "cbb": "basketball_ncaab",
}

_cache = TTLCache()
_ODDS_TTL = 120  # free-tier quota is scarce; don't hammer it


def is_configured():
    return bool(os.environ.get("ODDS_API_KEY"))


def fetch_odds(league):
    """Live moneyline (h2h) odds for a league from The Odds API, or None if
    no API key is configured. Each bookmaker's odds are de-vigged into a
    consensus implied probability per team.
    """
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        return None

    sport_key = SPORT_KEY_MAP.get(league)
    if not sport_key:
        return []

    cache_key = f"odds:{league}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    resp = requests.get(
        f"{ODDS_API_BASE}/sports/{sport_key}/odds/",
        params={
            "apiKey": api_key,
            "regions": "us",
            "markets": "h2h",
            "oddsFormat": "decimal",
        },
        timeout=10,
    )
    resp.raise_for_status()
    events = resp.json()

    parsed = []
    for event in events:
        team_probs = {}  # team name -> list of no-vig probabilities, one per bookmaker
        for bookmaker in event.get("bookmakers", []):
            h2h = next((m for m in bookmaker.get("markets", []) if m.get("key") == "h2h"), None)
            if not h2h:
                continue
            outcomes = h2h.get("outcomes", [])
            if len(outcomes) < 2:
                continue
            raw_probs = [1.0 / o["price"] for o in outcomes if o.get("price")]
            total = sum(raw_probs)
            if total <= 0:
                continue
            for outcome, raw_prob in zip(outcomes, raw_probs):
                novig = raw_prob / total
                team_probs.setdefault(outcome["name"], []).append(novig)

        consensus = {
            team: sum(probs) / len(probs) for team, probs in team_probs.items() if probs
        }
        if not consensus:
            continue

        parsed.append(
            {
                "home_team": event.get("home_team"),
                "away_team": event.get("away_team"),
                "commence_time": event.get("commence_time"),
                "book_count": max((len(v) for v in team_probs.values()), default=0),
                "consensus_prob": consensus,
            }
        )

    _cache.set(cache_key, parsed, _ODDS_TTL)
    return parsed
