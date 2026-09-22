"""Existing behavior — the feature build must not regress these."""

from shoplib.model import Cart, Product
from shoplib.pricing import apply_volume_discount
from shoplib.serializers import cart_lines


def _cart() -> Cart:
    c = Cart()
    c.add(Product(sku="MUG-1", price_cents=1200), qty=2)
    c.add(Product(sku="PEN-1", price_cents=300), qty=1)
    return c


def test_subtotal():
    assert _cart().subtotal_cents() == 2700


def test_add_rejects_zero_qty():
    import pytest

    with pytest.raises(ValueError):
        _cart().add(Product(sku="X", price_cents=1), qty=0)


def test_volume_discount_threshold():
    assert apply_volume_discount(2700, 3) == 2700
    assert apply_volume_discount(5000, 1) == 4500


def test_cart_lines_shape():
    lines = cart_lines(_cart())
    assert lines[0] == ("MUG-1", 2, 1200)
    assert len(lines) == 2
