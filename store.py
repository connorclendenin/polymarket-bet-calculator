"""Durable key/value storage for hosts with an ephemeral filesystem (Vercel
serverless): if UPSTASH_REDIS_REST_URL / _TOKEN are set, reads/writes go to
Upstash Redis (free tier, survives restarts/redeploys/cold starts) instead
of local disk. Same pattern as the sibling `portfolio-dashboard` /
`financial-growth-analyzer` projects, generalized into one shared helper
instead of being copy-pasted per call site. Falls back to a local JSON file
when Upstash isn't configured, so local dev behaves exactly as before —
zero behavior change without the env vars set.
"""

import json
import os
import urllib.error
import urllib.request

_UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
_UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def is_configured():
    return bool(_UPSTASH_URL and _UPSTASH_TOKEN)


def _upstash_request(path):
    req = urllib.request.Request(
        f"{_UPSTASH_URL}/{path}", headers={"Authorization": f"Bearer {_UPSTASH_TOKEN}"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _upstash_set(key, value_json):
    """SET via a POST body rather than embedding the value in the URL path
    (what `_upstash_request` does for every other command) — confirmed
    empirically that a large value (~450KB, this project's full cached
    scan) blows past a line-length limit when crammed into a GET request
    line (`LineTooLong`); the request body has no such practical limit."""
    req = urllib.request.Request(
        f"{_UPSTASH_URL}/set/{key}",
        data=value_json.encode("utf-8"),
        headers={"Authorization": f"Bearer {_UPSTASH_TOKEN}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _local_path(local_key):
    return os.path.join(DATA_DIR, f"{local_key}.json")


def load(redis_key, local_key, default):
    """Reads a JSON value, preferring Redis when configured, else a local
    file named `data/<local_key>.json`."""
    if is_configured():
        try:
            result = _upstash_request(f"get/{redis_key}")
            raw = result.get("result")
            if raw:
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            print(f"[store] Upstash load failed for key={redis_key!r}: HTTP {e.code} {body}")
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
            _upstash_set(redis_key, json.dumps(data))
        except urllib.error.HTTPError as e:
            # Surfaced (never the URL/token) so a real auth/format/size
            # problem shows up in logs instead of silently no-opping.
            body = e.read().decode("utf-8", "replace")[:300]
            print(f"[store] Upstash save failed for key={redis_key!r}: HTTP {e.code} {body}")
        except Exception as e:
            print(f"[store] Upstash save failed for key={redis_key!r}: {e!r}")

    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_local_path(local_key), "w") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass
