"""Tests for ordkit.year_phrase (num2words-backed year rendering)."""

from ordkit.ordkit import year_phrase


def test_recent_year():
    assert year_phrase(2023) == "twenty twenty-three"


def test_nineties_year():
    assert year_phrase(1999) == "nineteen ninety-nine"


def test_two_word_millennium():
    assert year_phrase(2001) == "two thousand and one"
