import time


class TTLCache:
    """LRU cache whose entries expire.

    TTLCache(max_size, ttl, clock=time.monotonic)
      * max_size < 1 or ttl <= 0 raise ValueError.
      * set(key, value): store; expires when clock() >= (clock() at set time) + ttl; the key becomes most
        recently used. If the number of live entries would exceed max_size, expired entries are purged
        first and then the least recently used entries are evicted.
      * get(key, default=None): the value, or `default` if missing or expired (an expired entry is removed).
        A successful get makes the key most recently used and does not extend its ttl.
      * len(cache) and `key in cache` ignore expired entries; `in` does not change recency.
      * purge(): remove expired entries, return how many were removed.
    """

    def __init__(self, max_size, ttl, clock=time.monotonic):
        raise NotImplementedError
