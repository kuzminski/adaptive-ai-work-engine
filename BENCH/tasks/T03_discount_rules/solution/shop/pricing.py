from decimal import ROUND_HALF_UP, Decimal

TAX_RATE = Decimal("0.23")
CENT = Decimal("0.01")


def cents(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def line_total(item):
    return Decimal(item["price"]) * item["qty"]


def subtotal(items):
    return sum((line_total(i) for i in items), Decimal("0"))


def eligible_subtotal(items):
    return subtotal(i for i in items if i.get("kind") != "gift_card")


def discount_rate(eligible):
    if eligible >= 250:
        return Decimal("0.15")
    if eligible >= 100:
        return Decimal("0.10")
    return Decimal("0")


def tax(amount):
    return cents(amount * TAX_RATE)
