from decimal import Decimal

from shop.cart import summary


def test_small_cart_totals():
    result = summary([{"price": "10.00", "qty": 2}])
    assert result["subtotal"] == Decimal("20.00")
    assert result["total"] == Decimal("24.60")
