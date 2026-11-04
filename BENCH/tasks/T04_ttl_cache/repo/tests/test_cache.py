import pytest

from shop.cache import TTLCache


def test_rejects_bad_arguments():
    with pytest.raises(ValueError):
        TTLCache(0, 10)
