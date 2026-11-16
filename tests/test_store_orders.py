"""Online store orders (Square Online) become jobs at Mock-Up Approved."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.urls import reverse

from apps.customers.models import Customer
from apps.integrations import nightly, orders, square
from apps.integrations.models import SquareOrder, WebhookEvent
from apps.jobs import services, timeline
from apps.jobs.models import Job, JobStatusHistory, PaymentStatus, Status
from apps.jobs.services import InvalidMove

pytestmark = pytest.mark.django_db


def order(
    *,
    id="ord-1",
    version=1,
    state="OPEN",
    source="Square Online",
    total=9000,
    due=0,
    customer_id="CUST9",
    reference="1001",
    location="LOC1",
    lines=None,
    refunds=None,
):
    data = {
        "id": id,
        "version": version,
        "location_id": location,
        "source": {"name": source},
        "reference_id": reference,
        "state": state,
        "customer_id": customer_id,
        "line_items": lines
        if lines is not None
        else [
            {
                "name": "Team hoodie",
                "variation_name": "Black / Large",
                "quantity": "2",
                "item_type": "ITEM",
                "modifiers": [{"name": "#12 SMITH"}],
            },
            {"name": "Team hoodie", "variation_name": "Black / Youth Medium", "quantity": "1"},
            {"name": "Sticker", "variation_name": "Regular", "quantity": "3", "item_type": "ITEM"},
            {"name": "Gift card", "quantity": "1", "item_type": "GIFT_CARD"},
        ],
        "fulfillments": [
            {
                "type": "SHIPMENT",
                "shipment_details": {
                    "recipient": {
                        "display_name": "Jo Buyer",
                        "email_address": "jo@example.com",
                        "phone_number": "403-555-0199",
                    }
                },
            }
        ],
        "total_money": {"amount": total, "currency": "CAD"},
        "net_amount_due_money": {"amount": due, "currency": "CAD"},
        "created_at": "2026-11-16T18:00:00Z",
        "updated_at": "2026-11-16T18:00:00Z",
    }
    if refunds is not None:
        data["refunds"] = refunds
    return data


@pytest.fixture(autouse=True)
def square_on(settings, monkeypatch):
    settings.SQUARE_ACCESS_TOKEN = "t"
    settings.SQUARE_WEBHOOK_SIGNATURE_KEY = "k"
    settings.SQUARE_ENABLED = True
    settings.SQUARE_LOCATION_ID = "LOC1"
    settings.SQUARE_STORE_SOURCES = ["Square Online"]
    fake = {"orders": {}, "customers": {}}
    monkeypatch.setattr(square, "get_order", lambda i: fake["orders"].get(i))
    monkeypatch.setattr(square, "get_customer", lambda i: fake["customers"].get(i))
    monkeypatch.setattr(square, "list_locations", lambda: ["LOC1"])
    monkeypatch.setattr(square, "list_invoices", lambda loc, **kw: iter([]))
    monkeypatch.setattr(
        square, "search_orders", lambda loc, **kw: iter(list(fake["orders"].values()))
    )
    return fake


def deliver(fake, data, event_id="ev-o1", kind="order.created"):
    fake["orders"][data["id"]] = data
    ev = WebhookEvent.objects.create(
        event_id=event_id,
        event_type=kind,
        square_object_id=data["id"],
        body={"type": kind, "data": {"id": data["id"], "object": {}}},
    )
    square.process_event(ev.pk)
    ev.refresh_from_db()
    return ev


# --- Sizes ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, size",
    [
        ("Large", "L"),
        ("l", "L"),
        ("X-Large", "XL"),
        ("XXL", "2XL"),
        ("3X", "3XL"),
        ("Youth Medium", "YM"),
        ("YS", "YS"),
        ("Y-XL", "YXL"),
        ("Black", None),
        ("Regular", None),
    ],
)
def test_sizes_from_words(text, size):
    assert orders.size_of(text) == size


def test_variation_splits_into_size_and_colour():
    assert orders.split_variation("Black / Large") == ("L", "Black")
    assert orders.split_variation("Medium, Heather Grey") == ("M", "Heather Grey")
    assert orders.split_variation("Regular") == (None, "")


# --- A store order arrives ------------------------------------------------------------


def test_a_store_order_becomes_a_job_at_mockup_approved(square_on):
    ev = deliver(square_on, order())
    assert ev.processed_at and ev.last_error == ""
    job = Job.objects.get()
    assert job.status == Status.MOCKUP_APPROVED and job.source == "online_store"
    assert job.fulfillment_method == "ship"
    assert job.due_date is not None and job.mockup_approved_at is not None
    assert job.title == "Store: Team hoodie + 1 more"  # two products; colours don't count
    assert job.square_number == "1001" and job.display_number == "1001"
    assert job.payment_status == PaymentStatus.PAID and job.amount_paid_cents == 9000
    history = job.history.get()
    assert history.source == JobStatusHistory.Source.SQUARE and "1001" in history.note

    hoodie, sticker = job.items.order_by("line_no")
    assert (hoodie.description, hoodie.color, hoodie.is_sized) == ("Team hoodie", "Black", True)
    assert {s.size: s.quantity for s in hoodie.sizes.all()} == {"L": 2, "YM": 1}
    assert "L ×2: #12 SMITH" in hoodie.notes
    assert not sticker.is_sized and {s.size: s.quantity for s in sticker.sizes.all()} == {"OS": 3}
    assert job.items.count() == 2  # the gift card isn't made


def test_the_buyer_becomes_the_customer(square_on):
    deliver(square_on, order())
    jo = Job.objects.get().customer
    assert (jo.display_name, jo.email, jo.phone) == ("Jo Buyer", "jo@example.com", "403-555-0199")
    assert jo.square_customer_id == "CUST9"


def test_a_known_buyer_is_reused(square_on):
    linked = Customer.objects.create(display_name="Jo B", square_customer_id="CUST9")
    deliver(square_on, order())
    assert Job.objects.get().customer == linked and Customer.objects.count() == 1


def test_a_buyer_matched_by_email_is_linked(square_on):
    jo = Customer.objects.create(display_name="Jo Buyer", email="JO@example.com")
    square_on["customers"]["CUST9"] = {"id": "CUST9", "email_address": "jo@example.com"}
    deliver(square_on, order())
    jo.refresh_from_db()
    assert Job.objects.get().customer == jo and jo.square_customer_id == "CUST9"


def test_counter_sales_are_left_alone(square_on):
    ev = deliver(square_on, order(source="Square Point of Sale"))
    assert ev.processed_at and ev.last_error == ""  # normal: no warning on the health panel
    assert not SquareOrder.objects.exists() and not Job.objects.exists()


def test_other_store_sources_can_be_added(square_on, settings):
    settings.SQUARE_STORE_SOURCES = ["Square Online", "Team Shop"]
    deliver(square_on, order(source="Graphnix Team Shop"))
    assert Job.objects.exists()


def test_a_draft_isnt_a_job_yet(square_on):
    deliver(square_on, order(state="DRAFT"))
    assert SquareOrder.objects.get().job is None and not Job.objects.exists()
    deliver(square_on, order(state="OPEN", version=2), event_id="ev-o2", kind="order.updated")
    assert Job.objects.count() == 1


def test_another_location_is_skipped(square_on):
    ev = deliver(square_on, order(location="ELSEWHERE"))
    assert "ELSEWHERE" in ev.last_error and not Job.objects.exists()


# --- Updates ----------------------------------------------------------------------


def test_updates_never_make_a_second_job(square_on):
    deliver(square_on, order())
    deliver(square_on, order(version=2, state="COMPLETED"), event_id="ev-o2", kind="order.updated")
    deliver(square_on, order(version=1), event_id="ev-o3", kind="order.updated")  # older
    assert Job.objects.count() == 1 and SquareOrder.objects.get().state == "COMPLETED"


def test_a_refund_shows_on_the_job(square_on):
    deliver(square_on, order())
    refund = [{"status": "COMPLETED", "amount_money": {"amount": 3000, "currency": "CAD"}}]
    deliver(square_on, order(version=2, refunds=refund), event_id="ev-o2", kind="order.updated")
    job = Job.objects.get()
    assert job.payment_status == PaymentStatus.REFUNDED


def test_a_cancelled_order_shows_cancelled(square_on):
    deliver(square_on, order())
    deliver(square_on, order(version=2, state="CANCELED"), event_id="ev-o2", kind="order.updated")
    assert Job.objects.get().payment_status == PaymentStatus.CANCELLED


def test_an_unpaid_order_waits_at_the_payment_gate(square_on, make_user):
    deliver(square_on, order(due=9000))
    job = Job.objects.get()
    assert job.payment_status == PaymentStatus.UNPAID
    with pytest.raises(InvalidMove):
        services.move(job, Status.MATERIALS_ORDERED, user=make_user("manager"))


def test_a_paid_order_can_move_straight_on(square_on, make_user):
    deliver(square_on, order())
    job = services.move(Job.objects.get(), Status.MATERIALS_ORDERED, user=make_user("manager"))
    assert job.status == Status.MATERIALS_ORDERED


# --- Missed webhooks and imports ------------------------------------------------------


def test_the_nightly_check_finds_a_missed_store_order(square_on):
    square_on["orders"]["ord-1"] = order()
    square_on["orders"]["pos-1"] = order(id="pos-1", source="Square Point of Sale")
    result = nightly.run()
    assert result["error"] == "" and result["orders"] == 1
    assert Job.objects.count() == 1


def test_the_import_lists_sources_and_imports(square_on):
    square_on["orders"]["ord-1"] = order()
    square_on["orders"]["pos-1"] = order(id="pos-1", source="Square Point of Sale")
    out = StringIO()
    call_command("import_square_orders", "--list", stdout=out)
    assert "Square Online  ← store" in out.getvalue() and "Square Point of Sale" in out.getvalue()
    out = StringIO()
    call_command("import_square_orders", "--latest", "5", stdout=out)
    assert "→ job" in out.getvalue() and Job.objects.count() == 1


# --- Who sees what ------------------------------------------------------------------


def test_the_office_sees_the_order_on_the_job(client, manager, square_on):
    deliver(square_on, order())
    job = Job.objects.get()
    client.force_login(manager)
    html = client.get(reverse("jobs:detail", args=[job.pk]) + "?tab=details").content.decode()
    assert "Store Order" in html and "1001" in html and "$90.00" in html
    assert "Square Online · Shipment" in html


def test_production_sees_the_job_but_no_money(client, make_user, square_on):
    deliver(square_on, order())
    job = Job.objects.get()
    client.force_login(make_user("production"))
    html = client.get(reverse("jobs:detail", args=[job.pk]) + "?tab=details").content.decode()
    assert "$90.00" not in html and "Team hoodie" in html
    assert not any("Payment" in e.title for e in timeline.build(job))


def test_a_long_product_name_is_cut_at_a_word():
    name = "SPECIAL. Youth and Unisex Hoodie. Tackle twill and embroidery full front. Free toque."
    items = [{"description": name}, {"description": name}]
    title = orders.title_for(items, limit=40)
    assert title == "Store: SPECIAL. Youth and Unisex Hoodie…" and "more" not in title


def test_an_order_number_already_on_a_job_isnt_reused(square_on, job_at):
    other = job_at(Status.QUOTING)
    other.square_number = "1001"
    other.save()
    deliver(square_on, order())
    job = Job.objects.exclude(pk=other.pk).get()
    assert job.square_number == "" and job.display_number.startswith("G-")
