import pytest

from shop.cache import TTLCache


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def make(max_size=3, ttl=10):
    clock = Clock()
    return TTLCache(max_size, ttl, clock=clock), clock


def test_bad_arguments():
    for args in ((0, 10), (-1, 10), (2, 0), (2, -5)):
        with pytest.raises(ValueError):
            TTLCache(*args)


def test_set_get_and_expiry_boundary():
    cache, clock = make(ttl=10)
    cache.set("a", 1)
    clock.now = 109.999
    assert cache.get("a") == 1
    clock.now = 110.0
    assert cache.get("a") is None
    assert cache.get("a", "dflt") == "dflt"
    assert len(cache) == 0


def test_get_does_not_extend_ttl_but_set_resets_it():
    cache, clock = make(ttl=10)
    cache.set("a", 1)
    clock.now = 105
    assert cache.get("a") == 1
    clock.now = 110
    assert cache.get("a") is None
    cache.set("b", 2)
    clock.now = 115
    cache.set("b", 3)
    clock.now = 124
    assert cache.get("b") == 3


def test_lru_eviction_and_recency_updates():
    cache, _ = make(max_size=2)
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1          # a is now most recent
    cache.set("c", 3)                   # evicts b
    assert "b" not in cache and "a" in cache and "c" in cache
    cache.set("a", 10)                  # overwrite makes a most recent
    cache.set("d", 4)                   # evicts c
    assert "c" not in cache and cache.get("a") == 10 and cache.get("d") == 4


def test_contains_does_not_touch_recency():
    cache, _ = make(max_size=2)
    cache.set("a", 1)
    cache.set("b", 2)
    assert "a" in cache
    cache.set("c", 3)                   # a is still least recent -> evicted
    assert "a" not in cache and "b" in cache


def test_expired_entries_are_purged_before_lru_eviction():
    cache, clock = make(max_size=2, ttl=10)
    cache.set("old", 1)
    clock.now = 105
    cache.set("fresh", 2)
    clock.now = 111                     # old expired, fresh alive
    cache.set("new", 3)                 # purging old makes room: fresh must survive
    assert "fresh" in cache and "new" in cache and "old" not in cache


def test_len_and_purge_count():
    cache, clock = make(max_size=5, ttl=10)
    cache.set("a", 1)
    clock.now = 104
    cache.set("b", 2)
    cache.set("c", 3)
    clock.now = 110
    assert cache.purge() == 1
    assert cache.purge() == 0
    assert len(cache) == 2


def test_len_ignores_expired_entries():
    cache, clock = make(max_size=5, ttl=10)
    cache.set("a", 1)
    clock.now = 104
    cache.set("b", 2)
    clock.now = 110
    assert len(cache) == 1
