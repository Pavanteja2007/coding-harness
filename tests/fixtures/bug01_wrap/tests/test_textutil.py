"""Tests for wrapwrap.textutil.wrap."""
from wrapwrap.textutil import wrap


def test_wrap_two_full_lines():
    assert wrap("aaa bbb ccc ddd", 7) == ["aaa bbb", "ccc ddd"]


def test_wrap_keeps_long_word_whole():
    assert wrap("abcdefghij", 4) == ["abcdefghij"]


def test_wrap_empty_text():
    assert wrap("", 10) == []


def test_wrap_short_trailing_line():
    # The final pending line is shorter than width — it must still appear.
    assert wrap("aaa bbb ccc", 7) == ["aaa bbb", "ccc"]
