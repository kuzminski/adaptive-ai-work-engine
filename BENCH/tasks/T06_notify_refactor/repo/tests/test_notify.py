from shop.notify import email_receipt


def test_receipt_golden():
    assert email_receipt("  ann lee ", 17, 12.5) == \
        "Subject: Receipt #17\n\nHello Ann Lee,\nyour order 17 totals 12.50.\nThanks!"
