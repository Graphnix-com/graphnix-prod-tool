"""Staging: clear_square_data removes what came from Square, keeps staff's own work."""

from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.customers.models import Customer
from apps.integrations.models import EstimateEmail, SquareEstimate, SquareInvoice, SquareOrder
from apps.jobs.models import Job, PaymentStatus, Status

pytestmark = pytest.mark.django_db


@pytest.fixture
def staging(settings):
    settings.APP_ENV = "staging"


@pytest.fixture
def data(customer):
    """A staff customer and job linked to a real invoice and estimate; Square-made customers
    and jobs."""
    staff_job = Job.objects.create(
        customer=customer,
        title="Staff tees",
        status=Status.INVOICED,
        square_number="000685",
        square_estimate_ref="000685",
        payment_status=PaymentStatus.PAID,
        amount_paid_cents=5000,
        amount_total_cents=5000,
    )
    SquareInvoice.objects.create(
        square_id="inv-1", invoice_number="002774", job=staff_job, total_cents=5000
    )
    SquareEstimate.objects.create(
        number="000685", job=staff_job, match_method=SquareEstimate.Match.CUSTOMER
    )
    EstimateEmail.objects.create(message_id="m1", kind="sent", received_at=timezone.now())

    imported = Customer.objects.create(display_name="Real Person", square_customer_id="SQ1")
    Job.objects.create(customer=imported, title="Their job")

    buyer = Customer.objects.create(display_name="Store Buyer", email="b@example.com")
    record(None, buyer, "create", {"source": [None, "Online store order"]})
    store_job = Job.objects.create(customer=buyer, title="Store: hoodie", square_number="1001")
    SquareOrder.objects.create(square_id="ord-1", reference="1001", job=store_job)

    new = Customer.objects.create(display_name="Estimate Person")
    record(None, new, "create", {"source": [None, "Square estimate 000700"]})
    est_job = Job.objects.create(customer=new, title="Quote", status=Status.QUOTE_SENT)
    SquareEstimate.objects.create(
        number="000700", job=est_job, match_method=SquareEstimate.Match.CREATED
    )
    return staff_job


def test_the_preview_removes_nothing(staging, data):
    out = StringIO()
    call_command("clear_square_data", stdout=out)
    text = out.getvalue()
    assert "Would remove" in text and "Nothing removed" in text
    assert "     3  customers from Square" in text and "     3  jobs" in text
    assert "Kept: 1 staff job" in text
    assert Customer.objects.count() == 4 and SquareInvoice.objects.exists()


def test_yes_removes_square_data_and_keeps_staff_work(staging, data, customer):
    call_command("clear_square_data", "--yes", stdout=StringIO())
    assert list(Customer.objects.all()) == [customer]
    assert list(Job.objects.all()) == [data]
    for model in (SquareInvoice, SquareOrder, SquareEstimate, EstimateEmail):
        assert not model.objects.exists()
    assert not AuditLog.objects.filter(entity_type="customers.customer").exclude(
        entity_id=customer.pk
    )
    data.refresh_from_db()
    assert data.payment_status == PaymentStatus.UNPAID and data.amount_paid_cents == 0
    assert data.square_number == "" and data.square_estimate_ref == ""
    assert data.display_number.startswith("G-") and data.status == Status.INVOICED


def test_staff_customers_and_jobs_without_square_are_untouched(staging, job_at):
    job = job_at(Status.QUOTING)
    call_command("clear_square_data", "--yes", stdout=StringIO())
    assert Job.objects.get() == job


def test_refused_on_production(settings, data):
    settings.APP_ENV = "production"
    with pytest.raises(CommandError, match="production"):
        call_command("clear_square_data", "--yes", stdout=StringIO())
    assert Customer.objects.count() == 4
