"""Business-day helpers (reference solution)."""
from datetime import timedelta


def is_business_day(day, holidays=()):
    return day.weekday() < 5 and day not in set(holidays)


def business_days_between(start, end, holidays=()):
    holidays = set(holidays)
    count = 0
    day = start
    while day < end:
        if is_business_day(day, holidays):
            count += 1
        day += timedelta(days=1)
    return count
