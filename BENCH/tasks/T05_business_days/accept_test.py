from datetime import date

import pytest

from shop.schedule import business_days_between, is_business_day
from shop.sla import due_date

MON = date(2026, 3, 2)      # a Monday


def test_weekends_and_holidays_are_not_business_days():
    assert is_business_day(MON)
    assert not is_business_day(date(2026, 3, 7))
    assert not is_business_day(date(2026, 3, 8))
    assert not is_business_day(MON, holidays=[MON])
    assert not is_business_day(MON, holidays=(MON,))
    assert is_business_day(date(2026, 3, 3), holidays={MON})


def test_between_is_half_open():
    assert business_days_between(MON, date(2026, 3, 9)) == 5
    assert business_days_between(MON, MON) == 0
    assert business_days_between(date(2026, 3, 9), MON) == 0
    assert business_days_between(date(2026, 3, 6), date(2026, 3, 9)) == 1       # Fri counted, Mon excluded
    assert business_days_between(date(2026, 3, 7), date(2026, 3, 9)) == 0       # Sat+Sun only


def test_between_with_holidays():
    assert business_days_between(MON, date(2026, 3, 9), holidays=[date(2026, 3, 4)]) == 4
    assert business_days_between(MON, date(2026, 3, 9), holidays=iter([date(2026, 3, 4), date(2026, 3, 5)])) == 3


def test_due_date_rules():
    assert due_date(MON, 0) == MON
    assert due_date(date(2026, 3, 7), 0) == date(2026, 3, 7)
    assert due_date(MON, 1) == date(2026, 3, 3)
    assert due_date(date(2026, 3, 6), 1) == date(2026, 3, 9)                    # Fri + 1 -> Mon
    assert due_date(date(2026, 3, 7), 1) == date(2026, 3, 9)                    # Sat + 1 -> Mon
    assert due_date(MON, 5) == date(2026, 3, 9)
    assert due_date(MON, 2, holidays=[date(2026, 3, 3)]) == date(2026, 3, 5)
    assert due_date(MON, 1, holidays={date(2026, 3, 3), date(2026, 3, 4)}) == date(2026, 3, 5)
    with pytest.raises(ValueError):
        due_date(MON, -1)
