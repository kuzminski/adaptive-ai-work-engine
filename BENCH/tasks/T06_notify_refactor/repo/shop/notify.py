def email_receipt(name, order_id, total):
    return "Subject: Receipt #%s\n\nHello %s,\nyour order %s totals %s.\nThanks!" % (
        order_id, name.strip().title(), order_id, "%.2f" % total)


def email_shipping(name, order_id, carrier):
    return "Subject: Order #%s shipped\n\nHello %s,\nyour order %s shipped with %s.\nThanks!" % (
        order_id, name.strip().title(), order_id, carrier.upper())


def email_refund(name, order_id, amount):
    return "Subject: Refund for #%s\n\nHello %s,\nwe refunded %s for order %s.\nThanks!" % (
        order_id, name.strip().title(), "%.2f" % amount, order_id)
