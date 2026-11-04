from shop import notify


def test_render_helper():
    assert notify._render("S", "a line", "  ann  ") == "Subject: S\n\nHello Ann,\na line\nThanks!"


def test_email_outputs_are_unchanged():
    assert notify.email_receipt("  ann lee ", 17, 12.5) == \
        "Subject: Receipt #17\n\nHello Ann Lee,\nyour order 17 totals 12.50.\nThanks!"
    assert notify.email_shipping("bob", 5, "dhl") == \
        "Subject: Order #5 shipped\n\nHello Bob,\nyour order 5 shipped with DHL.\nThanks!"
    assert notify.email_refund("cy", 9, 3) == \
        "Subject: Refund for #9\n\nHello Cy,\nwe refunded 3.00 for order 9.\nThanks!"


def test_emails_go_through_render(monkeypatch):
    monkeypatch.setattr(notify, "_render", lambda subject, line, name: "RENDERED")
    assert notify.email_receipt("a", 1, 1) == "RENDERED"
    assert notify.email_shipping("a", 1, "x") == "RENDERED"
    assert notify.email_refund("a", 1, 1) == "RENDERED"


def test_sms_receipt():
    assert notify.sms_receipt("  ann lee ", 17, 12.5) == "Hi Ann Lee: order 17 total 12.50"


def test_sms_is_cut_at_40_characters():
    text = notify.sms_receipt("a" * 50, 1, 1)
    assert len(text) == 40 and text.endswith("…") and text.startswith("Hi Aaaa")
    exactly = notify.sms_receipt("x" * 12, 12345, 12.5)     # 'Hi X...: order 12345 total 12.50' is 40 long
    assert len(exactly) == 40 and not exactly.endswith("…")
