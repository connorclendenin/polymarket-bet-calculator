import json
import re
import time
from datetime import datetime, timezone

import requests

from cache import TTLCache

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"

# Polymarket tag ids for each league, resolved via /tags/slug/<slug>.
LEAGUE_TAGS = {
    "nfl": 450,
    "nba": 745,
    "mlb": 100381,
    "nhl": 899,
    "cfb": 100351,
    "cbb": 101178,
}

_cache = TTLCache()
_EVENTS_TTL = 300  # game schedule/metadata barely changes within a window; refresh every 5 min
_BOOKS_TTL = 15  # live order books refresh fast
MAX_LIVE_HOURS = 5  # how long after kickoff a game still counts as "live" rather than over


def _get(url, params=None, timeout=10, retries=2):
    """Gamma occasionally resets the connection under rapid sequential
    requests (seen when paginating multiple leagues back-to-back) — a
    couple of quick retries clears it almost every time, so a scan doesn't
    hard-fail over a single transient blip."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(0.3 * (attempt + 1))
    raise last_exc


def _parse_json_field(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
    return value or []


def _fetch_league_events(league, tag_id, max_pages=3, page_size=100):
    """Gamma's /events caps each response at 100 regardless of `limit`, and
    for leagues with heavy season-long futures/props markets (NFL, CFB
    especially), those non-game markets — created back at the start of the
    season — swamp the first page or more when sorted oldest-first, pushing
    actual upcoming games off the page entirely. Sorting newest-created
    first (individual game markets are created shortly before each game, so
    they skew recent) surfaces real games far sooner. Confirmed empirically
    that 3 pages fully covers both NFL (actually stops adding new games
    after page 2) and CFB (the worst case — needs all 3), so that's the
    cap: enough for full coverage without hammering Gamma with requests it
    occasionally resets under rapid sequential pagination.
    """
    events = []
    offset = 0
    for page_num in range(max_pages):
        if page_num > 0:
            time.sleep(0.15)  # a short gap between pages of the same league avoids tripping Gamma's reset
        page = _get(
            f"{GAMMA_BASE}/events",
            params={
                "active": "true",
                "closed": "false",
                "limit": page_size,
                "offset": offset,
                "order": "startDate",
                "ascending": "false",
                "tag_id": tag_id,
            },
        )
        if not page:
            break
        events.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return events


def fetch_games(leagues=None):
    """Return the moneyline market for every upcoming or currently-live game
    across the requested leagues, with metadata needed for arb/odds/stats.
    """
    leagues = leagues or list(LEAGUE_TAGS.keys())
    now = datetime.now(timezone.utc)
    games = []
    seen_event_ids = set()

    for league in leagues:
        tag_id = LEAGUE_TAGS.get(league)
        if not tag_id:
            continue

        cache_key = f"events:{league}"
        try:
            events = _cache.get_or_set(
                cache_key, _EVENTS_TTL, lambda tid=tag_id, lg=league: _fetch_league_events(lg, tid)
            )
        except requests.exceptions.RequestException:
            # One league having a bad moment with Gamma shouldn't take the
            # whole scan down — skip it this round, the others still return,
            # and it'll retry fresh next poll (nothing is cached on failure).
            continue

        for event in events:
            event_id = event.get("id")
            if event_id in seen_event_ids:
                continue

            markets = event.get("markets") or []
            moneyline = next(
                (m for m in markets if m.get("sportsMarketType") == "moneyline"), None
            )
            if not moneyline:
                continue
            if not moneyline.get("active") or moneyline.get("closed"):
                continue

            game_start_raw = moneyline.get("gameStartTime") or event.get("startTime")
            if not game_start_raw:
                continue
            try:
                game_start = datetime.fromisoformat(
                    game_start_raw.replace(" ", "T").replace("+00", "+00:00")
                )
                if game_start.tzinfo is None:
                    game_start = game_start.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            hours_since_start = (now - game_start).total_seconds() / 3600
            if hours_since_start > MAX_LIVE_HOURS:
                continue  # long over; Polymarket just hasn't closed the market yet
            if not moneyline.get("acceptingOrders", True):
                continue

            outcomes = _parse_json_field(moneyline.get("outcomes"))
            token_ids = _parse_json_field(moneyline.get("clobTokenIds"))
            if len(outcomes) < 2 or len(token_ids) != len(outcomes):
                continue

            teams = event.get("teams") or []
            team_names = [t.get("name") for t in teams if t.get("name")]
            team_by_outcome = {}
            for team in teams:
                meta = {
                    "ordering": team.get("ordering"),
                    "logo": team.get("logo"),
                    "color": team.get("color"),
                    "record": team.get("record"),
                }
                for key in ("alias", "name", "abbreviation"):
                    val = team.get(key)
                    if val:
                        team_by_outcome[val.lower()] = meta

            seen_event_ids.add(event_id)
            game = {
                    "event_id": event_id,
                    "league": league,
                    "title": event.get("title"),
                    "slug": event.get("slug"),
                    "game_start": game_start.isoformat(),
                    "status": "live" if hours_since_start >= 0 else "upcoming",
                    "team_names": team_names,
                    "outcomes": [
                        {
                            "name": outcomes[i],
                            "token_id": token_ids[i],
                            "home_away": (team_by_outcome.get(outcomes[i].lower()) or {}).get(
                                "ordering"
                            ),
                            "logo": (team_by_outcome.get(outcomes[i].lower()) or {}).get("logo"),
                            "color": (team_by_outcome.get(outcomes[i].lower()) or {}).get(
                                "color"
                            ),
                            "record": (team_by_outcome.get(outcomes[i].lower()) or {}).get(
                                "record"
                            ),
                        }
                        for i in range(len(outcomes))
                    ],
                }

            if league == "nfl":
                game["totals"] = _extract_total_lines(markets, "totals")
                game["team_totals"] = {
                    name: _extract_total_lines(markets, "team_totals", team_label=name)
                    for name in outcomes
                }

            games.append(game)

    games.sort(key=lambda g: g["game_start"])
    return games


def _extract_total_lines(markets, sports_market_type, team_label=None):
    """Pulls every Over/Under line for a totals or team_totals market set
    (Polymarket lists one binary market per line, e.g. "O/U 46.5") into a
    list of {line, over_token_id, under_token_id, over_price, under_price}.
    team_label filters to one team's totals when given (team_totals covers
    both teams in one flat list, distinguished only by question text).
    """
    lines = []
    for m in markets:
        if m.get("sportsMarketType") != sports_market_type or not m.get("active"):
            continue
        if team_label and team_label.lower() not in (m.get("question") or "").lower():
            continue
        group_title = m.get("groupItemTitle") or ""
        match = re.search(r"(\d+(?:\.\d+)?)", group_title)
        if not match:
            continue
        line = float(match.group(1))
        outcomes = _parse_json_field(m.get("outcomes"))
        token_ids = _parse_json_field(m.get("clobTokenIds"))
        prices = _parse_json_field(m.get("outcomePrices"))
        if len(outcomes) != 2 or len(token_ids) != 2 or len(prices) != 2:
            continue
        idx = {name.lower(): i for i, name in enumerate(outcomes)}
        if "over" not in idx or "under" not in idx:
            continue
        lines.append(
            {
                "line": line,
                "over_token_id": token_ids[idx["over"]],
                "under_token_id": token_ids[idx["under"]],
                "over_price": float(prices[idx["over"]]),
                "under_price": float(prices[idx["under"]]),
            }
        )
    lines.sort(key=lambda x: x["line"])
    return lines


def _fetch_books_batch(token_ids):
    if not token_ids:
        return {}
    books = {}
    chunk_size = 150
    for i in range(0, len(token_ids), chunk_size):
        chunk = token_ids[i : i + chunk_size]
        payload = [{"token_id": tid} for tid in chunk]
        try:
            resp = requests.post(f"{CLOB_BASE}/books", json=payload, timeout=10)
            resp.raise_for_status()
            chunk_books = resp.json()
        except requests.exceptions.RequestException:
            # Same reasoning as the per-league skip above: a flaky CLOB
            # response for one chunk shouldn't crash the whole scan — those
            # tokens just come back with no price this round.
            continue
        for book in chunk_books:
            asset_id = book.get("asset_id")
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            best_bid = max((float(b["price"]) for b in bids), default=None)
            best_ask = min((float(a["price"]) for a in asks), default=None)
            books[asset_id] = {"best_bid": best_bid, "best_ask": best_ask}
    return books


def fetch_books(token_ids):
    """Live best-bid/best-ask per token id, batched and short-cached."""
    uncached = [t for t in token_ids if _cache.get(f"book:{t}") is None]
    if uncached:
        fresh = _fetch_books_batch(uncached)
        for tid, book in fresh.items():
            _cache.set(f"book:{tid}", book, _BOOKS_TTL)
    return {t: _cache.get(f"book:{t}") for t in token_ids}


def fetch_positions(address):
    data = _get(f"{DATA_API_BASE}/positions", params={"user": address, "limit": 200})
    return data


def fetch_portfolio_value(address):
    data = _get(f"{DATA_API_BASE}/value", params={"user": address})
    if isinstance(data, list) and data:
        return data[0].get("value")
    return None


_RESOLUTION_TTL = 300  # settlement checks are polled repeatedly; no need to hit this every time


def fetch_event_resolution(event_id):
    """For a game whose moneyline market has closed, returns the winning
    outcome's name (Polymarket collapses a resolved market's outcomePrices
    to exactly "1" for the winner, "0" for the rest). Returns None if the
    market isn't resolved yet."""

    def _load():
        data = _get(f"{GAMMA_BASE}/events/{event_id}")
        markets = data.get("markets") or []
        moneyline = next((m for m in markets if m.get("sportsMarketType") == "moneyline"), None)
        if not moneyline or not moneyline.get("closed"):
            return None
        outcomes = _parse_json_field(moneyline.get("outcomes"))
        prices = _parse_json_field(moneyline.get("outcomePrices"))
        if len(outcomes) != len(prices):
            return None
        for name, price in zip(outcomes, prices):
            try:
                if float(price) >= 0.99:
                    return name
            except (TypeError, ValueError):
                continue
        return None

    return _cache.get_or_set(f"resolution:{event_id}", _RESOLUTION_TTL, _load)


def fetch_prop_resolution(event_id, token_id):
    """For a total/team-total prop market (identified by one of its two
    token ids, since an event carries a whole ladder of parallel total-line
    markets, not just one), returns the winning side's name ("Over"/"Under")
    once that specific market has closed. Returns None if not resolved yet
    or the token can't be found on this event."""

    def _load():
        data = _get(f"{GAMMA_BASE}/events/{event_id}")
        markets = data.get("markets") or []
        for m in markets:
            token_ids = _parse_json_field(m.get("clobTokenIds"))
            if token_id not in token_ids:
                continue
            if not m.get("closed"):
                return None
            outcomes = _parse_json_field(m.get("outcomes"))
            prices = _parse_json_field(m.get("outcomePrices"))
            if len(outcomes) != len(prices):
                return None
            for name, price in zip(outcomes, prices):
                try:
                    if float(price) >= 0.99:
                        return name
                except (TypeError, ValueError):
                    continue
            return None
        return None

    return _cache.get_or_set(f"prop_resolution:{event_id}:{token_id}", _RESOLUTION_TTL, _load)


_ACTIVITY_TTL = 300  # history only grows; no need to refetch every positions load


def fetch_activity(address, max_entries=3000, page_size=500):
    """Full trade/redemption/rebate history for a wallet, paginated. This is
    the raw material for the realized P/L chart — Polymarket has no
    dedicated P/L-history endpoint, so it's reconstructed from this."""

    def _load():
        items = []
        offset = 0
        while len(items) < max_entries:
            page = _get(
                f"{DATA_API_BASE}/activity",
                params={"user": address, "limit": page_size, "offset": offset},
            )
            if not page:
                break
            items.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        return items

    return _cache.get_or_set(f"activity:{address}", _ACTIVITY_TTL, _load)
