from .pricing import cents, discount_rate, eligible_subtotal, subtotal, tax


def summary(items):
    sub = cents(subtotal(items))
    eligible = eligible_subtotal(items)
    discount = cents(eligible * discount_rate(eligible))
    t = tax(sub - discount)
    return {"subtotal": sub, "discount": discount, "tax": t, "total": cents(sub - discount + t)}
