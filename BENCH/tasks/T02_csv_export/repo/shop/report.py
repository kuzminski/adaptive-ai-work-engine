def to_rows(orders):
    """Plain tuples (id, customer, total) for each order dict."""
    return [(o["id"], o["customer"], o["total"]) for o in orders]
