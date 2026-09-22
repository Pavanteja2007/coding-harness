"""Catalog models for the shoplib package."""

from dataclasses import dataclass, field


@dataclass
class Product:
    """One catalog product: sku, unit price, and the cents-off coupon."""

    sku: str
    price_cents: int
    coupon_cents: int = 0


@dataclass
class Cart:
    """A customer's cart: ordered lines of (product, quantity)."""

    lines: list = field(default_factory=list)

    def add(self, product: Product, qty: int = 1) -> None:
        """Add qty of product to the cart."""
        if qty <= 0:
            raise ValueError("quantity must be positive")
        self.lines.append((product, qty))

    def subtotal_cents(self) -> int:
        """Pre-discount subtotal: sum of price * qty per line."""
        return sum(p.price_cents * q for p, q in self.lines)
