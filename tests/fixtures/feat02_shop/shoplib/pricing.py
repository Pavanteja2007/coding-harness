"""Pricing and loyalty rules applied to a cart at checkout."""

from shoplib.model import Cart


def apply_volume_discount(subtotal_cents: int, line_count: int) -> int:
    """The catalog's standing rule: 10% off any subtotal >= 5000 cents."""
    if subtotal_cents >= 5000:
        return subtotal_cents - subtotal_cents // 10
    return subtotal_cents
