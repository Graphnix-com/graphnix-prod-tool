"""Staff feedback, 19 Nov 2026: Mock-up Pending, invoice numbers, paid invoices, the
"no mock-up attached" choice, Approved by, Browse open jobs, and Shop notes."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.customers.models import Contact
from apps.integrations import linking, square
from apps.integrations.models import SquareInvoice
from apps.jobs import board, mockups, services, shopnotes
from apps.jobs.models import Job, MockupApproval, PaymentStatus, ShopNote, Status

from .conftest import approval_for
from .test_square_webhooks import invoice

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def square_on(settings):
    settings.SQUARE_ACCESS_TOKEN = ""
    settings.SQUARE_WEBHOOK_SIGNATURE_KEY = "k"
    settings.SQUARE_ENABLED = True
    settings.SQUARE_LOCATION_ID = "LOC1"


def test_the_stage_is_called_mockup_pending():
    assert Status.MOCKUP_SENT.label == "Mock-up Pending"


# --- Invoices -----------------------------------------------------------------------


def test_the_invoice_number_replaces_the_job_number(job_at):
    job = job_at(Status.QUOTE_SENT, square_number="E-77", square_estimate_ref="E-77")
    square.apply(invoice(title=f"{job.job_number} tees", number="000123"))
    job.refresh_from_db()
    assert job.display_number == "000123" and job.status == Status.INVOICED
    # The estimate number still finds it.
    assert list(board.search("E-77", can_see_contacts=False)) == [job]


def test_a_second_invoice_doesnt_renumber_the_job(job_at):
    job = job_at(Status.QUOTING)
    square.apply(invoice(id="inv-1", title=f"{job.job_number} deposit", number="000123"))
    square.apply(invoice(id="inv-2", title=f"{job.job_number} balance", number="000124"))
    job.refresh_from_db()
    assert job.display_number == "000123"


def test_a_number_another_job_has_is_left_alone(job_at):
    job_at(Status.QUOTING, square_number="000123")
    job = job_at(Status.QUOTING)
    square.apply(invoice(title=f"{job.job_number} tees", number="000123"))
    job.refresh_from_db()
    assert job.display_number == job.job_number


def test_unlinking_gives_back_the_estimate_number(manager, job_at):
    job = job_at(Status.QUOTE_SENT, square_estimate_ref="E-77", square_number="E-77")
    inv = square.apply(invoice(title=f"{job.job_number} tees", number="000123"))
    linking.unlink(inv, user=manager)
    job.refresh_from_db()
    assert job.display_number == "E-77"


def test_a_paid_invoice_moves_the_job_to_mockup_pending(job_at):
    job = job_at(Status.QUOTE_SENT)
    square.apply(invoice(title=f"{job.job_number} tees"))
    job.refresh_from_db()
    assert job.status == Status.INVOICED
    square.apply(invoice(title=f"{job.job_number} tees", version=2, status="PAID", paid=52500))
    job.refresh_from_db()
    assert job.status == Status.MOCKUP_SENT
    assert job.history.order_by("-pk").first().note == "Square invoice 000123 paid"


def test_a_deposit_doesnt_move_it_and_later_stages_stay(job_at):
    job = job_at(Status.INVOICED)
    square.apply(invoice(title=f"{job.job_number} tees", status="PARTIALLY_PAID", paid=10000))
    job.refresh_from_db()
    assert job.status == Status.INVOICED
    later = job_at(Status.MOCKUP_APPROVED)
    square.apply(invoice(id="inv-9", title=f"{later.job_number} x", status="PAID", paid=52500))
    later.refresh_from_db()
    assert later.status == Status.MOCKUP_APPROVED


def test_skip_mockup_from_mockup_pending(manager, job_at):
    pending = job_at(Status.MOCKUP_SENT, payment_status=PaymentStatus.PAID)
    job = services.skip_mockup(pending, user=manager, note="Reprint")
    assert job.status == Status.MATERIALS_ORDERED


# --- Mock-ups --------------------------------------------------------------------------


def test_anyone_who_moves_it_can_go_ahead_without_a_mockup(manager, job_at):
    job = services.move(job_at(Status.INVOICED), Status.MOCKUP_SENT, user=manager, mockup="none")
    assert job.history.get().note == "No mock-up attached"
    job = services.move(
        job,
        Status.MOCKUP_APPROVED,
        user=manager,
        mockup="none",
        approval={"approved_by": "Pat Doe", "ticked": mockups.checks()},
    )
    approval = MockupApproval.objects.get()
    assert approval.mockup is None and mockups.approved(job) is None
    note = job.history.order_by("-pk").first().note
    assert note.startswith("Mock-up approved by Pat Doe (no mock-up attached)")


def test_the_form_offers_no_mockup_and_fills_in_the_customer(client, manager, job_at, customer):
    job = job_at(Status.INVOICED)
    client.force_login(manager)
    html = client.get(reverse("jobs:detail", args=[job.pk])).content.decode()
    assert "No mock-up attached (move anyway)" in html and "Move To Mock-up Pending" in html
    approving = job_at(Status.MOCKUP_SENT)
    approval_for(approving)  # gives it a version
    html = client.get(reverse("jobs:detail", args=[approving.pk])).content.decode()
    assert 'value="Pat Doe (pat@example.com)"' in html


def test_approved_by_uses_the_contact_when_there_is_one(job_at, customer):
    contact = Contact.objects.create(customer=customer, full_name="Sam Lee", email="sam@x.com")
    assert mockups.approved_by_default(job_at(Status.MOCKUP_SENT, contact=contact)) == (
        "Sam Lee (sam@x.com)"
    )


# --- Browse open jobs ---------------------------------------------------------------------


def test_browse_open_jobs_links_the_picked_job(client, manager, job_at, customer):
    customer.square_customer_id = "CUST1"
    customer.save()
    job = job_at(Status.INVOICED)
    Job.objects.filter(pk=job.pk).update(title="Hoodies")
    inv = square.apply(invoice(title="No number here"))
    client.force_login(manager)
    html = client.get(reverse("integrations:inbox")).content.decode()
    assert "Browse open jobs" in html and "Same customer" in html and "Hoodies" in html
    client.post(reverse("integrations:invoice_link", args=[inv.pk]), {"job_id": job.pk})
    inv.refresh_from_db()
    assert inv.job == job


def test_browse_refuses_a_closed_job(client, manager, job_at):
    closed = job_at(Status.CLOSED)
    inv = square.apply(invoice(title="No number here"))
    client.force_login(manager)
    client.post(reverse("integrations:invoice_link", args=[inv.pk]), {"job_id": closed.pk})
    assert SquareInvoice.objects.get(pk=inv.pk).job is None


# --- Shop notes ------------------------------------------------------------------------


def test_anyone_adds_a_note_and_only_managers_tick_or_remove(client, make_user, manager, job_at):
    floor = make_user("production", "Riley")
    job = job_at(Status.IN_PRODUCTION)
    client.force_login(floor)
    client.post(reverse("jobs:shop_note_add"), {"text": "Reorder black ink", "job_number": ""})
    client.post(
        reverse("jobs:shop_note_add"), {"text": "Check the press", "job_number": job.job_number}
    )
    general, on_job = ShopNote.objects.order_by("pk")
    assert general.job is None and general.created_by == floor
    assert on_job.job == job
    html = client.get(reverse("jobs:list")).content.decode()
    assert "Reorder black ink" in html and "Riley" in html and "✓ Done" not in html
    client.post(reverse("jobs:shop_note_tick", args=[general.pk]))
    assert ShopNote.objects.get(pk=general.pk).done_at is None  # refused
    client.force_login(manager)
    client.post(reverse("jobs:shop_note_tick", args=[general.pk]))
    client.post(reverse("jobs:shop_note_remove", args=[on_job.pk]))
    general.refresh_from_db()
    assert general.done_by == manager and not ShopNote.objects.filter(pk=on_job.pk).exists()
    assert "Reorder black ink" not in client.get(reverse("jobs:list")).content.decode()


def test_a_note_cant_carry_a_price_or_an_unknown_job(manager):
    with pytest.raises(Exception, match="Leave prices out"):
        shopnotes.add(user=manager, text="Charge them $40 extra")
    with pytest.raises(Exception, match="No single job"):
        shopnotes.add(user=manager, text="Look at this", job_number="nope")


# --- Due date ---------------------------------------------------------------------------


def test_store_orders_still_get_a_due_date(customer):
    job = services.store_order_job(customer=customer, title="Store order", note="Paid")
    assert job.due_date is not None
    assert job.due_date > timezone.localdate() + timedelta(days=14)
    assert Job.objects.get(pk=job.pk).status == Status.MOCKUP_APPROVED
