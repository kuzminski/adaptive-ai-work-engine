def _render(subject, line, name):
    return "Subject: %s\n\nHello %s,\n%s\nThanks!" % (subject, name.strip().title(), line)


def email_receipt(name, order_id, total):
    return _render("Receipt #%s" % order_id, "your order %s totals %s." % (order_id, "%.2f" % total), name)


def email_shipping(name, order_id, carrier):
    return _render("Order #%s shipped" % order_id, "your order %s shipped with %s." % (order_id, carrier.upper()), name)


def email_refund(name, order_id, amount):
    return _render("Refund for #%s" % order_id, "we refunded %s for order %s." % ("%.2f" % amount, order_id), name)


def sms_receipt(name, order_id, total):
    text = "Hi %s: order %s total %s" % (name.strip().title(), order_id, "%.2f" % total)
    return text if len(text) <= 40 else text[:39] + "…"
