"""Tests for cartlib.cart."""
from cartlib.cart import Cart, price_report


def test_add_item():
    c = Cart()
    c.add_item("apple", 1.5, quantity=2)
    assert c.items == {"apple": 2}
    assert c.total == 3.0


def test_price_report_isolated():
    # Each default call must see only the current cart — no leaking lines.
    c1 = Cart()
    c1.add_item("apple", 1.5)
    assert price_report(c1) == ["apple x1"]

    c2 = Cart()
    c2.add_item("banana", 0.5)
    assert price_report(c2) == ["banana x1"]


def test_remove_item():
    c = Cart()
    c.add_item("apple", 1.0, quantity=3)
    c.remove_item("apple", 2)
    assert c.items == {"apple": 1}
    c.remove_item("apple", 5)
    assert c.items == {}


def test_item_count():
    c = Cart()
    c.add_item("a", 1.0, quantity=2)
    c.add_item("b", 2.0, quantity=3)
    assert c.item_count() == 5
