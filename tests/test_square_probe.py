"""square_probe: what Square's API shows changed recently (read only)."""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.integrations import square

pytestmark = pytest.mark.django_db


def stamp(minutes_ago):
    return (timezone.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def fake(settings, monkeypatch):
    settings.SQUARE_ACCESS_TOKEN = "t"
    settings.SQUARE_LOCATION_ID = "LOC1"
    seen = {}
    draft = {
        "id": "ord-draft",
        "state": "DRAFT",
        "source": {"name": "Square Estimates"},
        "total_money": {"amount": 4500, "currency": "CAD"},
        "line_items": [{"name": "Tee"}],
        "created_at": stamp(5),
        "updated_at": stamp(5),
    }
    invoices = [
        {"id": "inv:new", "invoice_number": "000300", "status": "DRAFT", "updated_at": stamp(5)},
        {"id": "inv:old", "invoice_number": "000100", "status": "PAID", "updated_at": stamp(600)},
    ]

    def search_orders(loc, **kw):
        seen["states"] = kw.get("states")
        return iter([draft])

    monkeypatch.setattr(square, "list_locations", lambda: ["LOC1"])
    monkeypatch.setattr(square, "search_orders", search_orders)
    monkeypatch.setattr(square, "list_invoices", lambda loc, **kw: iter(invoices))
    monkeypatch.setattr(
        square,
        "search_customers",
        lambda **kw: iter([{"id": "C1", "given_name": "Test", "updated_at": stamp(4)}]),
    )
    monkeypatch.setattr(square, "get_order", lambda i: draft if i == "ord-draft" else None)
    monkeypatch.setattr(square, "get_invoice", lambda i: invoices[0] if i == "inv:new" else None)
    return seen


def probe(*args):
    out = StringIO()
    call_command("square_probe", *args, stdout=out)
    return out.getvalue()


def test_lists_recent_orders_invoices_and_customers(fake):
    out = probe("--minutes", "30")
    assert "ord-draft  DRAFT  source=Square Estimates" in out
    assert "inv:new" in out and "inv:old" not in out
    assert "C1  Test" in out
    assert "DRAFT" in fake["states"]  # drafts are asked for


def test_show_prints_one_in_full():
    assert '"source"' in probe("--show", "ord-draft")
    assert '"invoice_number": "000300"' in probe("--show", "inv:new")
    with pytest.raises(CommandError, match="no order or invoice"):
        probe("--show", "nope")
