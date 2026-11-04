from decimal import Decimal

TAX_RATE = Decimal("0.23")


def line_total(item):
    return Decimal(item["price"]) * item["qty"]


def subtotal(items):
    return sum((line_total(i) for i in items), Decimal("0"))


def tax(amount):
    return amount * TAX_RATE
