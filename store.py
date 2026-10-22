"""Durable key/value storage for hosts with an ephemeral filesystem (Vercel
serverless): if UPSTASH_REDIS_REST_URL / _TOKEN are set, reads/writes go to
Upstash Redis (free tier, survives restarts/redeploys/cold starts) instead
of local disk. Same pattern as the sibling `portfolio-dashboard` /
`financial-growth-analyzer` projects, generalized into one shared helper
instead of being copy-pasted per call site. Falls back to a local JSON file
when Upstash isn't configured, so local dev behaves exactly as before —
zero behavior change without the env vars set.

Uses `requests` (already a dependency, already proven reliable on this
project's own Vercel deployment for every other outbound call) rather than
the stdlib `urllib.request` — that was the first implementation here, and
it failed in production with `URLError(OSError(16, 'Device or resource
busy'))` on every single request, which `requests`/urllib3's different
connection handling doesn't hit.
"""

import json
import os

import requests

def _clean_env(value):
    """Strips whitespace and any stray leading/trailing quote characters —
    both confirmed empirically as copy-paste artifacts from pasting an env
    var field straight out of a `KEY="value"`-style code snippet (Upstash's
    own REST API console shows credentials exactly that way): a trailing
    newline makes the HTTP client reject the Authorization header outright,
    and a lone trailing `"` (confirmed: no matching leading quote — the
    paste started after the `="`) baked into the URL makes it resolve to a
    bogus hostname with a literal %22 in it instead of a connection
    failure that would at least point at the real cause. Strips quotes
    unconditionally (paired, single, or absent) rather than only a matched
    pair, since the real failure was exactly the unpaired case."""
    return value.strip().strip("\"'").strip()


_UPSTASH_URL = _clean_env(os.environ.get("UPSTASH_REDIS_REST_URL", "")).rstrip("/")
_UPSTASH_TOKEN = _clean_env(os.environ.get("UPSTASH_REDIS_REST_TOKEN", ""))

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

_HEADERS = {"Authorization": f"Bearer {_UPSTASH_TOKEN}"}


def is_configured():
    return bool(_UPSTASH_URL and _UPSTASH_TOKEN)


def _local_path(local_key):
    return os.path.join(DATA_DIR, f"{local_key}.json")


def load(redis_key, local_key, default):
    """Reads a JSON value, preferring Redis when configured, else a local
    file named `data/<local_key>.json`."""
    if is_configured():
        try:
            resp = requests.get(f"{_UPSTASH_URL}/get/{redis_key}", headers=_HEADERS, timeout=10)
            resp.raise_for_status()
            raw = resp.json().get("result")
            if raw:
                return json.loads(raw)
        except Exception as e:
            print(f"[store] Upstash load failed for key={redis_key!r}: {e!r}")
        # falls through to file storage below either way

    try:
        with open(_local_path(local_key)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(redis_key, local_key, data):
    """Writes a JSON value to Redis when configured; always also writes the
    local file (cheap, and keeps local dev + the git-committed state from
    the GitHub Actions cron working the same way they already do)."""
    if is_configured():
        try:
            # POST with the value in the body rather than a GET with it
            # embedded in the URL — confirmed empirically that a large
            # value (~450KB, this project's full cached scan) blows past a
            # request-line length limit when crammed into a URL.
            resp = requests.post(
                f"{_UPSTASH_URL}/set/{redis_key}",
                data=json.dumps(data).encode("utf-8"),
                headers=_HEADERS,
                timeout=15,
            )
            resp.raise_for_status()
        except Exception as e:
            # Surfaced (never the URL/token) so a real auth/format/size
            # problem shows up in logs instead of silently no-opping.
            print(f"[store] Upstash save failed for key={redis_key!r}: {e!r}")

    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_local_path(local_key), "w") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def delete(redis_key, local_key):
    """Removes a key entirely (Redis, when configured, and the local file)
    — e.g. retiring a renamed/removed strategy's state rather than leaving
    it as orphaned data nothing reads anymore."""
    if is_configured():
        try:
            resp = requests.post(f"{_UPSTASH_URL}/del/{redis_key}", headers=_HEADERS, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            print(f"[store] Upstash delete failed for key={redis_key!r}: {e!r}")

    try:
        os.remove(_local_path(local_key))
    except OSError:
        pass
