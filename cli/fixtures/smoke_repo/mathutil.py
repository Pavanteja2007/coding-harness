"""Small math helpers (fixture module with a real bug)."""


def mean(values):
    """Return the arithmetic mean of a list of numbers.

    BUG: currently returns the sum instead of the mean.
    """
    return sum(values)


def median(values):
    """Return the median of a sorted-agnostic list of numbers."""
    s = sorted(values)
    n = len(s)
    if n == 0:
        raise ValueError("median of empty list")
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2
