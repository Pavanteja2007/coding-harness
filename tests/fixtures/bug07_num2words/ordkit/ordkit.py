"""Year-to-phrase rendering using num2words (an external library)."""

from num2words import num2words


def year_phrase(year: int) -> str:
    """Render a calendar year the way people say it.

    >>> year_phrase(2023)
    'twenty twenty-three'
    >>> year_phrase(1999)
    'nineteen ninety-nine'
    """
    # BUG: uses the default cardinal converter, which spells years as
    # plain big numbers ("two thousand and twenty-three") instead of the
    # human year form. The library does year rendering through a
    # dedicated converter selected by its `to=` argument.
    return num2words(year)
