import pytest

from shop.inventory import Inventory
from shop.models import OutOfStock


def stocked():
    inv = Inventory()
    inv.add("x", 5)
    return inv


def test_available_reflects_reservations():
    inv = stocked()
    inv.reserve("x", 2)
    assert inv.available("x") == 3
    assert inv.available("nope") == 0


def test_reserve_checks_available_not_on_hand_and_keeps_state_on_failure():
    inv = stocked()
    inv.reserve("x", 4)
    with pytest.raises(OutOfStock):
        inv.reserve("x", 2)
    assert inv.available("x") == 1
    with pytest.raises(OutOfStock):
        inv.reserve("unknown", 1)
    for bad in (0, -1):
        with pytest.raises(ValueError):
            inv.reserve("x", bad)
    assert inv.available("x") == 1


def test_release_frees_exactly_once():
    inv = stocked()
    rid = inv.reserve("x", 3)
    inv.release(rid)
    assert inv.available("x") == 5
    with pytest.raises(KeyError):
        inv.release(rid)
    assert inv.available("x") == 5


def test_commit_sells_and_returns_quantity():
    inv = stocked()
    rid = inv.reserve("x", 3)
    assert inv.commit(rid) == 3
    assert inv.available("x") == 2
    other = inv.reserve("x", 2)
    assert inv.available("x") == 0
    inv.release(other)
    assert inv.available("x") == 2
    with pytest.raises(KeyError):
        inv.commit(rid)
    with pytest.raises(KeyError):
        inv.release(rid)
    with pytest.raises(KeyError):
        inv.commit(999)
