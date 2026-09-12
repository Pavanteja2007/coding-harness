"""Serialize invoices to plain dict / CSV row shapes."""

from invlib.model import Invoice


def to_dict(inv: Invoice) -> dict:
    """Plain-dict shape for JSON responses."""
    return {"number": inv.number, "total": inv.invoice_total(include_tax=True)}


def to_csv_row(inv: Invoice) -> tuple:
    """Two-column CSV row: (number, total)."""
    return (inv.number, str(inv.invoice_total(include_tax=True)))
