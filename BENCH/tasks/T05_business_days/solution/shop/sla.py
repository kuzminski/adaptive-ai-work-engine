"""SLA due dates (reference solution)."""
from datetime import timedelta

from .schedule import is_business_day


def due_date(start, business_days, holidays=()):
    if business_days < 0:
        raise ValueError("business_days must be >= 0")
    holidays = set(holidays)
    day, remaining = start, business_days
    while remaining > 0:
        day += timedelta(days=1)
        if is_business_day(day, holidays):
            remaining -= 1
    return day
