from decimal import Decimal

from invlib.api import format_invoice_line
from invlib.model import Invoice
from invlib.reports import render_summary
from invlib.serializers import to_csv_row, to_dict


def _inv() -> Invoice:
    return Invoice(number="INV-041", subtotal=Decimal("100.00"), tax=Decimal("8.25"))


def test_amount_due_includes_tax():
    """The core regression: the final amount owed MUST include assessed
    tax (the public contract of every consumer in this codebase)."""
    assert _inv().amount_due() == Decimal("108.25")


def test_serializers_carry_the_full_total():
    assert to_dict(_inv())["total"] == Decimal("108.25")


def test_csv_row_carries_the_full_total():
    assert to_csv_row(_inv())[1] == "108.25"


def test_report_shows_the_full_total():
    assert "108.25" in render_summary(_inv())


def test_list_line_shows_the_full_total():
    assert "108.25" in format_invoice_line(_inv())
