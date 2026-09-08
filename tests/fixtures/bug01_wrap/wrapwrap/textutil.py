"""Wrapping utilities for the wrapwrap package."""
from typing import List


def wrap(text: str, width: int = 10) -> List[str]:
    """Wrap `text` into lines of at most `width` characters.

    Words longer than `width` are kept whole on their own line.

    >>> wrap("aaa bbb ccc ddd", 7)
    ['aaa bbb', 'ccc ddd']
    """
    if width <= 0:
        raise ValueError("width must be positive")
    words = text.split()
    lines: List[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    if lines and current and len(current) == width:
        lines.append(current)
    return lines
