"""Single-line invoice rendering for list views."""

from invlib.model import Invoice


def format_invoice_line(inv: Invoice) -> str:
    """One compact line per invoice in the overview table."""
    return f"{inv.number} | total {inv.invoice_total()}"
