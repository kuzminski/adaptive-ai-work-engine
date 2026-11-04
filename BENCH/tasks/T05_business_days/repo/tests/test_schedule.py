from datetime import date

from shop.schedule import is_business_day


def test_monday_is_a_business_day():
    assert is_business_day(date(2026, 3, 2))
