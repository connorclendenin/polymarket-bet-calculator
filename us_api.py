"""Authenticated client for Connor's own polymarket.us account — balances
and positions, read-only. Separate from polymarket.py (which talks to the
original, public, address-based Polymarket product): polymarket.us is a
different product/backend with its own Ed25519-signed REST API, documented
at docs.polymarket.us/api-reference.

Requires POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY in the environment
(set them in .env — never pass real values into code or chat). The signing
key is the user's own account credential, generated at polymarket.us/developer
and revocable there at any time.
"""

import base64
import os
import time

import requests

try:
    from cryptography.hazmat.primitives.asymmetric import ed25519
except ImportError:
    ed25519 = None

from cache import TTLCache

US_API_BASE = "https://api.polymarket.us"
_cache = TTLCache()
_ACCOUNT_TTL = 20  # /api/scan and /api/paper both poll roughly every 30s; avoid double-hitting


def is_configured():
    return bool(os.environ.get("POLYMARKET_KEY_ID")) and bool(
        os.environ.get("POLYMARKET_SECRET_KEY")
    )


def dependency_missing():
    """True if the keys are set but the signing library isn't installed —
    lets the app tell the user exactly what's missing rather than just
    failing silently."""
    return is_configured() and ed25519 is None


def _signing_key():
    secret = os.environ["POLYMARKET_SECRET_KEY"]
    raw = base64.b64decode(secret)[:32]
    return ed25519.Ed25519PrivateKey.from_private_bytes(raw)


def _auth_headers(method, path):
    key_id = os.environ["POLYMARKET_KEY_ID"]
    private_key = _signing_key()
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method}{path}"
    signature = base64.b64encode(private_key.sign(message.encode())).decode()
    return {
        "X-PM-Access-Key": key_id,
        "X-PM-Timestamp": timestamp,
        "X-PM-Signature": signature,
        "Content-Type": "application/json",
    }


def _get(path, params=None):
    resp = requests.get(
        US_API_BASE + path, headers=_auth_headers("GET", path), params=params, timeout=10
    )
    resp.raise_for_status()
    return resp.json()


def fetch_balances():
    def _load():
        data = _get("/v1/account/balances")
        return data.get("balances", [])

    return _cache.get_or_set("balances", _ACCOUNT_TTL, _load)


def fetch_buying_power():
    balances = fetch_balances()
    usd = next((b for b in balances if b.get("currency") == "USD"), None)
    usd = usd or (balances[0] if balances else None)
    return usd.get("buyingPower") if usd else None


def fetch_positions(limit=100):
    """Paginates through all open positions. Response shape per the docs:
    {"positions": {<market_slug>: <position>}, "nextCursor", "eof"} — the
    exact fields on each <position> object weren't shown in the reference
    (collapsed behind "Show child attributes"), so this returns them as-is
    rather than guessing field names; the Flask route below reads from
    whatever keys are actually present.
    """
    def _load():
        all_positions = {}
        cursor = None
        while True:
            params = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            data = _get("/v1/portfolio/positions", params=params)
            all_positions.update(data.get("positions") or {})
            if data.get("eof", True) or not data.get("nextCursor"):
                break
            cursor = data["nextCursor"]
        return all_positions

    return _cache.get_or_set("positions", _ACCOUNT_TTL, _load)


_ACTIVITIES_TTL = 60  # full paginated history; don't refetch on every 20s account poll


def fetch_activities(limit=100, max_entries=3000):
    """Full activity history: trades, position resolutions, deposits,
    withdrawals, transfers, bonuses. Used to reconstruct capital flow —
    see performance.build_us_capital_series for how (and why only some
    activity types are used for that)."""

    def _load():
        items = []
        cursor = None
        while len(items) < max_entries:
            params = {"limit": limit, "sortOrder": "SORT_ORDER_ASCENDING"}
            if cursor:
                params["cursor"] = cursor
            data = _get("/v1/portfolio/activities", params=params)
            items.extend(data.get("activities") or [])
            if data.get("eof", True) or not data.get("nextCursor"):
                break
            cursor = data["nextCursor"]
        return items

    return _cache.get_or_set("activities", _ACTIVITIES_TTL, _load)


def get_account_summary():
    return {
        "balances": fetch_balances(),
        "positions": fetch_positions(),
    }
