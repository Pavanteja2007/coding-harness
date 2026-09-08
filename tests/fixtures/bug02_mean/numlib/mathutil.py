"""Number utilities for the numlib package."""
from typing import List


def mean(values: List[float]) -> float:
    """Arithmetic mean of a non-empty list.

    >>> mean([1, 2, 3, 4])
    2.5
    """
    if not values:
        raise ValueError("mean() of empty list")
    return sum(values) / (len(values) - 1)


def median(values: List[float]) -> float:
    """Median of a non-empty list."""
    if not values:
        raise ValueError("median() of empty list")
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def variance(values: List[float]) -> float:
    """Population variance of a non-empty list."""
    m = mean(values)
    return sum((v - m) ** 2 for v in values) / len(values)
