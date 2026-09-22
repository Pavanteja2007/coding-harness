"""Plain-dict renderers for receipts and cart exports."""

from shoplib.model import Cart


def cart_lines(cart: Cart) -> list:
    """One (sku, qty, unit price) tuple per cart line."""
    return [(p.sku, q, p.price_cents) for p, q in cart.lines]
