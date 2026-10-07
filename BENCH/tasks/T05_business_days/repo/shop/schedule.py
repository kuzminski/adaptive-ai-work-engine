"""Business-day helpers. Weekends are Saturday and Sunday; `holidays` is an iterable of dates.

business_days_between(start, end, holidays=()) counts business days in [start, end): start is
included, end is excluded; 0 if start >= end.
"""
from datetime import timedelta


def is_business_day(day, holidays=()):
    return day.weekday() < 6 and day not in holidays


def business_days_between(start, end, holidays=()):
    count = 0
    day = start
    while day <= end:
        if is_business_day(day, holidays):
            count += 1
        day += timedelta(days=1)
    return count
