"""AP Top 25 college football rankings, from ESPN's public (keyless)
rankings endpoint. Used to gate NCAAF paper-trading bets to ranked
matchups only — Connor's explicit ask, after noticing most of his CFB
paper-trading losses were on unranked teams (small/mid-major games the
model's CFB signal — which has no real team-stats feed, see stats.py's
own docstring — is least reliable on).
"""

import requests

import matcher
from cache import TTLCache

RANKINGS_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/college-football/rankings"
)

_cache = TTLCache()
_RANKINGS_TTL = 21600  # 6h — the AP poll only updates weekly; no need to refetch often


def _fetch_top25_names():
    """Each ranked team's location + nickname, normalized — both forms ESPN
    and Polymarket's own team-name strings are likely to use (e.g. "Boise
    St" vs "Boise State")."""
    resp = requests.get(RANKINGS_URL, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    ap_poll = next((r for r in data.get("rankings", []) if r.get("type") == "ap"), None)
    if not ap_poll:
        return set()
    names = set()
    for entry in ap_poll.get("ranks", []):
        team = entry.get("team") or {}
        for key in ("location", "nickname"):
            val = team.get(key)
            if val:
                names.add(matcher._normalize(val))
    return names


def top25_names():
    try:
        return _cache.get_or_set("cfb_top25", _RANKINGS_TTL, _fetch_top25_names)
    except Exception:
        return set()  # best-effort — a fetch failure shouldn't block the whole scan


def is_ranked_team(team_name):
    """Exact match on normalized name, deliberately not substring overlap —
    "Iowa" being ranked must never make "Northern Iowa" look ranked, and
    "Georgia" must never make "Georgia Tech" look ranked. A name that
    doesn't match any ranked team's exact name is treated as unranked,
    even if it's a real ranked team under a form this doesn't recognize —
    missing a genuinely-ranked game is a far safer failure than wrongly
    including an unranked one, which is the whole point of this filter."""
    return matcher._normalize(team_name) in top25_names()


def is_ranked_matchup(team_names):
    """True if either side of a CFB game is currently AP Top 25 ranked."""
    return any(is_ranked_team(name) for name in team_names)
