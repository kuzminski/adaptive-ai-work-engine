import csv
import io


def to_rows(orders):
    """Plain tuples (id, customer, total) for each order dict."""
    return [(o["id"], o["customer"], o["total"]) for o in orders]


def to_csv(orders):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["id", "customer", "total"])
    for order_id, customer, total in to_rows(orders):
        writer.writerow([order_id, customer, f"{total:.2f}"])
    return buffer.getvalue()
