import time
from collections import OrderedDict


class TTLCache:
    """LRU cache whose entries expire (reference solution)."""

    def __init__(self, max_size, ttl, clock=time.monotonic):
        if max_size < 1 or ttl <= 0:
            raise ValueError("max_size must be >= 1 and ttl > 0")
        self._max, self._ttl, self._clock = max_size, ttl, clock
        self._data = OrderedDict()

    def _expired(self, expiry):
        return self._clock() >= expiry

    def purge(self):
        dead = [k for k, (_, exp) in self._data.items() if self._expired(exp)]
        for key in dead:
            del self._data[key]
        return len(dead)

    def set(self, key, value):
        self._data.pop(key, None)
        self._data[key] = (value, self._clock() + self._ttl)
        if len(self._data) > self._max:
            self.purge()
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def get(self, key, default=None):
        entry = self._data.get(key)
        if entry is None:
            return default
        if self._expired(entry[1]):
            del self._data[key]
            return default
        self._data.move_to_end(key)
        return entry[0]

    def __contains__(self, key):
        entry = self._data.get(key)
        if entry is None:
            return False
        if self._expired(entry[1]):
            del self._data[key]
            return False
        return True

    def __len__(self):
        self.purge()
        return len(self._data)
