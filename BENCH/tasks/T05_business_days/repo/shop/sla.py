"""SLA due dates. due_date(start, n, holidays=()): the date n business days after `start`.

n == 0 returns start unchanged; start itself is never counted; n < 0 raises ValueError.
"""
from datetime import timedelta

from .schedule import is_business_day


def due_date(start, business_days, holidays=()):
    day, remaining = start, business_days
    while remaining > 0:
        day += timedelta(days=1)
        if is_business_day(day, holidays):
            remaining -= 1
    return day
