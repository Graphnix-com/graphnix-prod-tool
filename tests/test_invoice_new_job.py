"""Create a job from an unlinked Square invoice, from the Invoices screen."""

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.customers.models import Customer
from apps.integrations import square
from apps.integrations.models import SquareInvoice
from apps.jobs.models import Job, PaymentStatus, Status

from .test_square_webhooks import invoice

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def square_on(settings):
    settings.SQUARE_ACCESS_TOKEN = ""
    settings.SQUARE_WEBHOOK_SIGNATURE_KEY = "k"
    settings.SQUARE_ENABLED = True
    settings.SQUARE_LOCATION_ID = "LOC1"


@pytest.fixture
def unlinked():
    data = invoice(title="Team hoodies", status="PAID", paid=52500)
    return square.apply(data)  # recipient: Pat Doe, Square customer CUST1


def url(inv):
    return reverse("integrations:invoice_new_job", args=[inv.pk])


def test_the_inbox_has_the_button(client, manager, unlinked):
    client.force_login(manager)
    html = client.get(reverse("integrations:inbox")).content.decode()
    assert url(unlinked) in html and "Create job" in html


def test_uses_the_customer_linked_in_square(client, manager, unlinked):
    pat = Customer.objects.create(display_name="Pat Doe", square_customer_id="CUST1")
    client.force_login(manager)
    assert "Linked to this invoice" in client.get(url(unlinked)).content.decode()
    response = client.post(url(unlinked), {"title": "Team hoodies, 40"})
    job = Job.objects.get()
    assert response["Location"] == reverse("jobs:detail", args=[job.pk])
    assert job.customer == pat and job.title == "Team hoodies, 40"
    # Already paid: straight on to Mock-up Pending, numbered after the invoice.
    assert job.status == Status.MOCKUP_SENT and job.payment_status == PaymentStatus.PAID
    unlinked.refresh_from_db()
    assert job.display_number == unlinked.invoice_number
    assert unlinked.job == job and unlinked.match_method == SquareInvoice.Match.MANUAL
    assert AuditLog.objects.filter(entity_type="jobs.job", action="create", actor=manager).exists()


def test_picks_a_same_name_customer_and_links_it_to_square(client, manager, unlinked):
    Customer.objects.create(display_name="Pat Doe", email="b@example.com")
    chosen = Customer.objects.create(display_name="pat doe", email="a@example.com")
    client.force_login(manager)
    html = client.get(url(unlinked)).content.decode()
    assert html.index("a@example.com") < html.index("b@example.com")
    client.post(url(unlinked), {"title": "x", "customer": chosen.pk})
    chosen.refresh_from_db()
    assert Job.objects.get().customer == chosen and chosen.square_customer_id == "CUST1"


def test_creates_a_new_customer_when_none_fits(client, manager, unlinked):
    client.force_login(manager)
    client.post(url(unlinked), {"title": "", "customer": "new"})
    job = Job.objects.get()
    assert job.customer.display_name == "Pat Doe" and job.title == "Team hoodies"
    assert job.customer.square_customer_id == "CUST1"


def test_a_made_up_customer_id_is_refused(client, manager, unlinked):
    other = Customer.objects.create(display_name="Someone Else")
    client.force_login(manager)
    client.post(url(unlinked), {"title": "x", "customer": other.pk})
    assert not Job.objects.exists()


def test_an_invoice_already_on_a_job_is_refused(client, manager, unlinked, job_at):
    from apps.integrations import linking

    linking.link(unlinked, job_at(Status.QUOTING), user=manager)
    client.force_login(manager)
    client.post(url(unlinked), {"title": "x"})
    assert Job.objects.count() == 1


def test_production_cant(client, make_user, unlinked):
    client.force_login(make_user("production"))
    assert client.get(url(unlinked)).status_code == 403
    assert client.post(url(unlinked), {"title": "x"}).status_code == 403
    assert not Job.objects.exists()
