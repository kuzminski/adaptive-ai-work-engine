import pytest

from shop.ledger import reconcile


def test_negative_window_is_rejected():
    with pytest.raises(ValueError):
        reconcile([], [], max_days=-1)
