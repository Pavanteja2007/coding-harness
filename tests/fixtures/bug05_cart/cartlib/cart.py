"""A tiny shopping cart for the cartlib package."""
from typing import Dict, List, Optional

# Shared mutable default — the classic Python gotcha this repo's bug is
# about: one list created at import time, appended to by every call that
# omits `report`.
_DEFAULT_REPORT: List[str] = []


class Cart:
    """In-memory shopping cart with item counts and a running total."""

    def __init__(self) -> None:
        self.items: Dict[str, int] = {}
        self.total: float = 0.0

    def add_item(self, name: str, price: float, quantity: int = 1) -> None:
        """Add `quantity` x `name` at `price` each."""
        if name not in self.items:
            self.items[name] = 0
        self.items[name] += quantity
        self.total += price * quantity

    def remove_item(self, name: str, quantity: int = 1) -> None:
        """Remove up to `quantity` units of `name`. No-op if absent."""
        if name not in self.items:
            return
        self.items[name] = max(0, self.items[name] - quantity)
        if self.items[name] == 0:
            del self.items[name]

    def item_count(self) -> int:
        """Total number of item units in the cart."""
        return sum(self.items.values())


def price_report(cart: Cart, report: Optional[List[str]] = None) -> List[str]:
    """Return "name xN" lines for the cart's contents.

    Called without an explicit `report`, each call must return ONLY the
    current cart's lines.
    """
    if report is None:
        report = _DEFAULT_REPORT
    for name, count in cart.items.items():
        report.append(f"{name} x{count}")
    return report
