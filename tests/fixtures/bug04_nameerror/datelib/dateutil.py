"""Calendar utilities for the datelib package."""
from typing import Dict

_DAYS_PER_MONTH: Dict[int, int] = {
    1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30,
    7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31,
}


def is_leap_year(year: int) -> bool:
    """True if `year` is a leap year (Gregorian rules)."""
    if year % 4 != 0:
        return False
    if year % 100 != 0:
        return True
    return year % 400 == 0


def days_in_month(year: int, month: int) -> int:
    """Number of days in `month` (1-12) of `year`."""
    if month == 2 and is_leap_year(year):
        return 29
    return _DAYS_PER_MONTHS[month]
