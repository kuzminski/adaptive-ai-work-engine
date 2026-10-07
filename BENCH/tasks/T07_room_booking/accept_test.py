import pytest

from shop.rooms import assign


def b(id, start, end, priority=1):
    return {"id": id, "start": start, "end": end, "priority": priority}


def test_validation():
    with pytest.raises(ValueError):
        assign([], 0)
    with pytest.raises(ValueError):
        assign([b("a", 5, 5)], 1)
    with pytest.raises(ValueError):
        assign([b("a", 6, 5)], 1)
    with pytest.raises(ValueError):
        assign([b("a", 1, 2), b("a", 3, 4)], 1)


def test_touching_intervals_share_a_room_and_lowest_index_wins():
    out = assign([b("a", 0, 5), b("b", 5, 8), b("c", 1, 3)], 2)
    assert out == {"assignments": {"a": 0, "c": 1, "b": 0}, "rejected": []}


def test_rejection_when_full_and_not_higher_priority():
    out = assign([b("a", 0, 10, 5), b("b", 1, 4, 5), b("c", 2, 3, 5)], 2)
    assert out == {"assignments": {"a": 0, "b": 1}, "rejected": ["c"]}


def test_eviction_picks_lowest_priority_then_latest_end_then_highest_room():
    out = assign([b("a", 0, 10, 2), b("b", 0, 20, 2), b("hi", 1, 5, 3)], 2)
    # a and b tie on priority 2; b ends later -> b is evicted and hi takes room 1
    assert out == {"assignments": {"a": 0, "hi": 1}, "rejected": ["b"]}
    out = assign([b("a", 0, 10, 1), b("b", 0, 10, 2), b("hi", 1, 5, 3)], 2)
    # b (higher priority) is processed first and takes room 0; a is the lowest priority and is evicted from room 1
    assert out == {"assignments": {"b": 0, "hi": 1}, "rejected": ["a"]}
    out = assign([b("a", 0, 10, 2), b("b", 0, 10, 2), b("hi", 1, 5, 3)], 2)
    # full tie on priority and end -> highest room index (room 1) is evicted
    assert out == {"assignments": {"a": 0, "hi": 1}, "rejected": ["b"]}


def test_processing_order_prefers_priority_then_id_at_equal_start():
    out = assign([b("z", 0, 5, 1), b("m", 0, 5, 9), b("a", 0, 5, 9)], 2)
    assert out == {"assignments": {"a": 0, "m": 1}, "rejected": ["z"]}


def test_evicted_room_can_be_evicted_again_and_order_of_rejection_is_kept():
    out = assign([b("a", 0, 10, 1), b("b", 1, 10, 2), b("c", 2, 10, 3)], 1)
    assert out == {"assignments": {"c": 0}, "rejected": ["a", "b"]}


def test_equal_priority_never_evicts():
    out = assign([b("a", 0, 10, 4), b("b", 1, 5, 4)], 1)
    assert out == {"assignments": {"a": 0}, "rejected": ["b"]}
