import time


class TTLCache:
    def __init__(self):
        self._store = {}

    def get(self, key):
        entry = self._store.get(key)
        if not entry:
            return None
        expires_at, value = entry
        if time.time() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key, value, ttl_seconds):
        self._store[key] = (time.time() + ttl_seconds, value)

    def get_or_set(self, key, ttl_seconds, fn):
        cached = self.get(key)
        if cached is not None:
            return cached
        value = fn()
        self.set(key, value, ttl_seconds)
        return value
