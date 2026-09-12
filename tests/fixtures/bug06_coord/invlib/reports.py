"""Human-readable invoice summaries."""

from invlib.model import Invoice


def render_summary(inv: Invoice) -> str:
    """The summary line bundle shown on the invoice page."""
    return f"Invoice {inv.number}\nTotal: {inv.invoice_total(include_tax=True)}"
