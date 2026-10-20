from datetime import datetime, timedelta, timezone

MATCH_WINDOW_HOURS = 30


def _normalize(name):
    return (name or "").lower().strip()


def _team_in_name(short_name, full_name):
    short_name = _normalize(short_name)
    full_name = _normalize(full_name)
    if not short_name or not full_name:
        return False
    return short_name in full_name or full_name in short_name


def find_odds_event(polymarket_team_names, game_start_iso, odds_events):
    """Match a Polymarket game (by its full team names + kickoff time) to the
    corresponding event from The Odds API, which uses its own full team names.
    Matching is name-overlap + a generous time window, since the two sources
    don't share any common id.
    """
    if not odds_events:
        return None

    try:
        game_start = datetime.fromisoformat(game_start_iso)
    except ValueError:
        return None

    best = None
    for odds_event in odds_events:
        try:
            commence = datetime.fromisoformat(
                odds_event["commence_time"].replace("Z", "+00:00")
            )
        except (KeyError, ValueError):
            continue

        if abs((commence - game_start).total_seconds()) > MATCH_WINDOW_HOURS * 3600:
            continue

        home = odds_event.get("home_team")
        away = odds_event.get("away_team")
        matches = sum(
            1
            for pm_team in polymarket_team_names
            if _team_in_name(pm_team, home) or _team_in_name(pm_team, away)
        )
        if matches >= 2:
            best = odds_event
            break

    return best
