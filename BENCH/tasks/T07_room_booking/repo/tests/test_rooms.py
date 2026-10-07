import pytest

from shop.rooms import assign


def test_rooms_must_be_positive():
    with pytest.raises(ValueError):
        assign([], 0)
