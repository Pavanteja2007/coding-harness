"""Tests for datelib.dateutil."""
import pytest

from datelib.dateutil import days_in_month, is_leap_year


@pytest.mark.parametrize("month,days", [
    (1, 31), (4, 30), (6, 30), (9, 30), (11, 30), (12, 31),
])
def test_days_in_month_fixed(month, days):
    assert days_in_month(2023, month) == days


def test_days_in_month_february():
    assert days_in_month(2023, 2) == 28


def test_days_in_month_leap_february():
    assert days_in_month(2024, 2) == 29


def test_leap_years():
    assert is_leap_year(2024)
    assert not is_leap_year(2023)
    assert not is_leap_year(1900)
    assert is_leap_year(2000)
