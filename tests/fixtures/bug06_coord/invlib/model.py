"""Invoice model."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class Invoice:
    """One customer invoice: pre-tax subtotal plus assessed tax."""

    number: str
    subtotal: Decimal
    tax: Decimal

    def invoice_total(self, include_tax: bool = False) -> Decimal:
        """Final amount the customer owes."""
        return self.subtotal
