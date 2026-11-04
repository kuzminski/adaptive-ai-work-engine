from decimal import Decimal

from shop.cart import summary
from shop.pricing import discount_rate


def D(x):
    return Decimal(x)


def check(items, subtotal, discount, tax, total):
    result = summary(items)
    assert set(result) == {"subtotal", "discount", "tax", "total"}
    assert [str(result[k]) for k in ("subtotal", "discount", "tax", "total")] == [subtotal, discount, tax, total]


def test_rate_boundaries():
    assert discount_rate(D("249.99")) == D("0.10")
    assert discount_rate(D("250")) == D("0.15")
    assert discount_rate(D("100")) == D("0.10")
    assert discount_rate(D("99.99")) == D("0")
    assert discount_rate(D("0")) == D("0")


def test_gift_cards_are_excluded_from_the_discount_base():
    check([{"price": "60.00", "qty": 2, "kind": "book"}, {"price": "50.00", "qty": 1, "kind": "gift_card"}],
          "170.00", "12.00", "36.34", "194.34")


def test_half_up_rounding_of_discount_and_tax():
    check([{"price": "33.35", "qty": 3, "kind": "toy"}], "100.05", "10.01", "20.71", "110.75")


def test_top_tier_and_tax_rounding():
    check([{"price": "250.00", "qty": 1}], "250.00", "37.50", "48.88", "261.38")


def test_no_discount_below_the_first_tier():
    check([{"price": "99.99", "qty": 1}], "99.99", "0.00", "23.00", "122.99")


def test_only_gift_cards_and_empty_cart():
    check([{"price": "300.00", "qty": 1, "kind": "gift_card"}], "300.00", "0.00", "69.00", "369.00")
    check([], "0.00", "0.00", "0.00", "0.00")
