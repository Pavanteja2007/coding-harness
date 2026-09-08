"""Tests for numlib.mathutil."""
import pytest

from numlib.mathutil import mean, median, variance


def test_mean_even_count():
    assert mean([1, 2, 3, 4]) == 2.5


def test_mean_single_element():
    # len-1 == 0 here — buggy code even hits ZeroDivisionError.
    assert mean([10]) == 10.0


def test_mean_empty_raises():
    with pytest.raises(ValueError):
        mean([])


def test_median_odd():
    assert median([3, 1, 2]) == 2


def test_median_even():
    assert median([4, 1, 3, 2]) == 2.5


def test_variance():
    assert variance([1, 2, 3, 4]) == pytest.approx(1.25)
