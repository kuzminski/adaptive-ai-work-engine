from .pricing import subtotal, tax


def summary(items):
    sub = subtotal(items)
    t = tax(sub)
    return {"subtotal": sub, "tax": t, "total": sub + t}
