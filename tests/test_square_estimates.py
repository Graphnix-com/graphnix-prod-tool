"""Square estimates, read from Square's emails (its API has no estimates)."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.customers.models import Customer
from apps.integrations import estimates, square
from apps.integrations.models import EstimateEmail, SquareEstimate, SquareInvoice
from apps.jobs.models import Job, JobStatusHistory, Status

from .test_square_webhooks import invoice

pytestmark = pytest.mark.django_db

SENT_SUBJECT = "A new estimate was sent to Josh Wier (#000685)"
ACCEPTED_SUBJECT = "You accepted an estimate! (#000685)"


def body(kind="New Estimate", number="000685", total="1.05", email="josh@example.com"):
    """Square's email as Graph gives it in text form."""
    first = "sent" if kind == "New Estimate" else "last updated"
    return f"""GRAPHNIX.COM LTD
{kind}
${total}
Estimate
View Estimate<https://squareup.com/estimate/x>
Estimate #{number} {first}
November 16, 2026

Customer
Josh Wier
{email}<mailto:{email}>

Message
Order will be processed once FULL PAYMENT is received. Etransfer can be sent to
jenn@graphnix.com. Current turnaround is 15 - 20 business days.

Embroidery
$1.00
Subtotal
$1.00
Included Tax ($0.05)
GST (5%)
$0.05
Total
${total}
Square Canada, Inc.
"""


def message(subject=SENT_SUBJECT, text=None, id="m1", at=None):
    return {
        "id": id,
        "subject": subject,
        "receivedDateTime": at or timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "from": {"emailAddress": {"address": "invoicing@messaging.squareup.com"}},
        "body": {"contentType": "text", "content": text if text is not None else body()},
    }


def accepted_message(id="m2", **kw):
    return message(ACCEPTED_SUBJECT, body("Estimate Accepted", **kw), id=id)


@pytest.fixture
def josh():
    return Customer.objects.create(
        display_name="Josh Wier", email="JOSH@example.com", square_customer_id="SQJ"
    )


@pytest.fixture(autouse=True)
def square_on(settings):
    settings.SQUARE_ACCESS_TOKEN = ""
    settings.SQUARE_WEBHOOK_SIGNATURE_KEY = "k"
    settings.SQUARE_ENABLED = True
    settings.SQUARE_LOCATION_ID = "LOC1"


# --- Reading the emails ----------------------------------------------------------------


def test_reads_a_sent_email():
    assert estimates.parse(SENT_SUBJECT, body()) == {
        "kind": "sent",
        "number": "000685",
        "customer_name": "Josh Wier",
        "customer_email": "josh@example.com",
        "total_cents": 105,
    }


def test_reads_an_accepted_email():
    parsed = estimates.parse(ACCEPTED_SUBJECT, body("Estimate Accepted", total="1,250.00"))
    assert parsed["kind"] == "accepted" and parsed["number"] == "000685"
    assert parsed["customer_name"] == "Josh Wier" and parsed["total_cents"] == 125000


def test_other_square_mail_is_not_an_estimate():
    assert estimates.parse("Receipt from GRAPHNIX.COM LTD", "Total $5.00") is None


# --- Sent ------------------------------------------------------------------------------


def test_a_new_customer_gets_a_new_job_at_quote_sent():
    estimates.handle(message())
    job = Job.objects.get()
    assert job.status == Status.QUOTE_SENT and job.quote_sent_at is not None
    assert job.square_number == "000685" and job.display_number == "000685"
    assert job.square_estimate_ref == "000685"
    assert job.customer.display_name == "Josh Wier" and job.customer.email == "josh@example.com"
    assert job.history.get().source == JobStatusHistory.Source.SQUARE
    assert SquareEstimate.objects.get().match_method == SquareEstimate.Match.CREATED


def test_the_customers_one_quoting_job_takes_the_estimate(josh, job_at):
    job = job_at(Status.QUOTING)
    job.customer = josh
    job.save()
    estimates.handle(message())
    job.refresh_from_db()
    assert job.status == Status.QUOTE_SENT and job.square_number == "000685"
    assert Job.objects.count() == 1
    assert SquareEstimate.objects.get().match_method == SquareEstimate.Match.CUSTOMER


def test_a_number_typed_on_the_job_matches_first(job_at):
    job = job_at(Status.QUOTING)
    job.square_estimate_ref = "000685"
    job.save()
    estimates.handle(message())
    job.refresh_from_db()
    assert job.status == Status.QUOTE_SENT and job.square_number == "000685"


def test_two_quoting_jobs_wait_to_be_linked(client, manager, josh, job_at):
    a, b = job_at(Status.QUOTING), job_at(Status.QUOTING)
    for j in (a, b):
        j.customer = josh
        j.save()
    estimates.handle(message())
    estimate = SquareEstimate.objects.get()
    assert estimate.job is None and list(estimates.waiting()) == [estimate]

    client.force_login(manager)
    html = client.get(reverse("integrations:inbox")).content.decode()
    assert "000685" in html and 'id="estimates"' in html
    client.post(
        reverse("integrations:estimate_link", args=[estimate.pk]), {"job_number": a.job_number}
    )
    a.refresh_from_db()
    assert a.status == Status.QUOTE_SENT and a.square_number == "000685"
    assert not estimates.waiting().exists()


def test_each_email_is_handled_once():
    estimates.handle(message())
    assert estimates.handle(message()) is None
    assert Job.objects.count() == 1 and EstimateEmail.objects.count() == 1


def test_other_square_mail_is_recorded_and_ignored():
    estimates.handle(message("Receipt from GRAPHNIX.COM LTD", "Total $5.00", id="r1"))
    assert EstimateEmail.objects.get().kind == "other" and not Job.objects.exists()


# --- Accepted --------------------------------------------------------------------------


def test_acceptance_moves_the_job_to_quote_approved():
    estimates.handle(message())
    estimates.handle(accepted_message())
    job = Job.objects.get()
    assert job.status == Status.QUOTE_APPROVED and job.quote_approved_at is not None


def test_acceptance_without_the_sent_email_still_works():
    estimates.handle(accepted_message())
    job = Job.objects.get()
    assert job.status == Status.QUOTE_APPROVED and job.square_number == "000685"


def test_a_job_already_past_quotes_stays_put(josh, job_at):
    job = job_at(Status.INVOICED)
    job.square_estimate_ref = "000685"
    job.save()
    estimates.handle(message())
    estimates.handle(accepted_message())
    job.refresh_from_db()
    assert job.status == Status.INVOICED


# --- The invoice Square makes on acceptance -----------------------------------------------


def accepted_invoice(**kw):
    data = invoice(id="inv-est", number="002774", title="Embroidery for Josh", total=105, **kw)
    data["primary_recipient"] = {"customer_id": "SQJ", "given_name": "Josh", "family_name": "Wier"}
    data["created_at"] = timezone.now().isoformat()
    return data


def test_the_new_invoice_links_by_customer_and_total(josh):
    estimates.handle(message())
    estimates.handle(accepted_message(email="josh@example.com"))
    stored = square.apply(accepted_invoice())
    job = Job.objects.get()
    assert stored.job == job and stored.match_method == SquareInvoice.Match.ESTIMATE
    job.refresh_from_db()
    assert job.status == Status.INVOICED


def test_an_invoice_that_arrives_first_links_on_acceptance(josh):
    estimates.handle(message())
    stored = square.apply(accepted_invoice())
    assert stored.job is None  # not accepted yet: it waits
    estimates.handle(accepted_message())
    stored.refresh_from_db()
    assert stored.job == Job.objects.get()
    assert Job.objects.get().status == Status.INVOICED


def test_a_different_total_doesnt_link(josh):
    estimates.handle(message())
    estimates.handle(accepted_message())
    data = accepted_invoice()
    data["payment_requests"][0]["computed_amount_money"]["amount"] = 999
    assert square.apply(data).job is None


# --- Reading the mailbox -----------------------------------------------------------------


@pytest.fixture
def mailbox(settings, monkeypatch):
    from apps.m365 import graph

    settings.ESTIMATE_MAILBOX = "info@graphnix.com"
    settings.M365_CONFIGURED = True
    box = {"messages": [message(), accepted_message()], "since": []}

    def list_messages(mailbox, *, senders, since):
        box["since"].append(since)
        yield from box["messages"]

    monkeypatch.setattr(graph, "list_messages", list_messages)
    return box


def test_check_mail_handles_new_messages_and_remembers(mailbox):
    result = estimates.check_mail()
    assert result["read"] == 2 and result["error"] == ""
    assert Job.objects.get().status == Status.QUOTE_APPROVED
    again = estimates.check_mail()
    assert again["read"] == 0
    assert mailbox["since"][1] < timezone.now() - timedelta(hours=23)  # a day of overlap


def test_a_mailbox_error_shows_on_the_health_panel(mailbox, monkeypatch):
    from apps.core import health
    from apps.m365 import graph

    def broken(*a, **kw):
        raise graph.GraphError("Reading info@graphnix.com failed (403)")
        yield  # pragma: no cover

    monkeypatch.setattr(graph, "list_messages", broken)
    estimates.check_mail()
    check = health.check_estimate_mail()
    assert check.status == health.FAIL and "403" in check.detail


def test_graph_only_opens_squares_messages(monkeypatch):
    from apps.m365 import graph

    calls = []

    class Response:
        def __init__(self, data):
            self.status_code, self._data = 200, data

        def json(self):
            return self._data

    listing = {
        "value": [
            {"id": "a", "from": {"emailAddress": {"address": "customer@example.com"}}},
            {"id": "b", "from": {"emailAddress": {"address": "Invoicing@messaging.squareup.com"}}},
        ]
    }

    def get(url, **kw):
        calls.append(url)
        return Response(listing if url.endswith("/messages") else {"id": url.rsplit("/", 1)[1]})

    monkeypatch.setattr(graph, "get_token", lambda: "t")
    monkeypatch.setattr(graph.requests, "get", get)
    got = list(
        graph.list_messages(
            "info@graphnix.com",
            senders=["invoicing@messaging.squareup.com"],
            since=timezone.now(),
        )
    )
    assert [m["id"] for m in got] == ["b"]
    assert not any(c.endswith("/messages/a") for c in calls)


# --- Screens -----------------------------------------------------------------------------


def test_follow_up_lists_old_unanswered_quotes(client, manager):
    estimates.handle(message())
    job = Job.objects.get()
    client.force_login(manager)
    assert (
        job.display_number
        not in client.get(reverse("home"))
        .content.decode()
        .split("Follow Up")[1]
        .split("</section>")[0]
    )
    Job.objects.filter(pk=job.pk).update(quote_sent_at=timezone.now() - timedelta(days=8))
    follow = client.get(reverse("home")).content.decode().split("Follow Up")[1]
    assert job.display_number in follow.split("</section>")[0]


def test_create_a_job_from_a_waiting_estimate(client, manager, josh, job_at):
    for _ in range(2):
        j = job_at(Status.QUOTING)
        j.customer = josh
        j.save()
    estimates.handle(message())
    estimate = SquareEstimate.objects.get()
    client.force_login(manager)
    url = reverse("integrations:estimate_new_job", args=[estimate.pk])
    assert "Josh Wier" in client.get(url).content.decode()
    client.post(url, {"title": "Embroidered caps", "customer": josh.pk})
    job = Job.objects.get(square_number="000685")
    assert job.status == Status.QUOTE_SENT and job.title == "Embroidered caps"


def test_set_aside(client, manager, josh, job_at):
    for _ in range(2):
        j = job_at(Status.QUOTING)
        j.customer = josh
        j.save()
    estimates.handle(message())
    estimate = SquareEstimate.objects.get()
    client.force_login(manager)
    client.post(reverse("integrations:estimate_set_aside", args=[estimate.pk]))
    assert not estimates.waiting().exists()


def test_production_cant_handle_estimates(client, make_user, josh, job_at):
    for _ in range(2):
        j = job_at(Status.QUOTING)
        j.customer = josh
        j.save()
    estimates.handle(message())
    estimate = SquareEstimate.objects.get()
    client.force_login(make_user("production"))
    assert (
        client.get(reverse("integrations:estimate_new_job", args=[estimate.pk])).status_code == 403
    )
    client.post(reverse("integrations:estimate_set_aside", args=[estimate.pk]))
    assert estimates.waiting().exists()
