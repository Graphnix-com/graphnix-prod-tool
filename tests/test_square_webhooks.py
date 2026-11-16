"""Step 2 week 1: Square webhooks, invoices and payment status (INT-01, FR-04 to FR-06, NFR-12).

Payloads follow Square's documented webhook format (event_id, type, data.id,
data.object.invoice). CI has no Square account, so the API client is never called here
unless a test switches it on with a fake.
"""

import base64
import hashlib
import hmac
import json

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.integrations import square, tasks
from apps.integrations.models import SquareInvoice, WebhookEvent
from apps.jobs import timeline
from apps.jobs.models import JobStatusHistory, PaymentStatus, Status

pytestmark = pytest.mark.django_db

KEY = "test-signature-key"
URL = "https://staging.jobs.graphnix.com/integrations/square/webhook/"


@pytest.fixture(autouse=True)
def square_on(settings):
    settings.SQUARE_WEBHOOK_SIGNATURE_KEY = KEY
    settings.SQUARE_WEBHOOK_URL = URL
    settings.SQUARE_ACCESS_TOKEN = ""  # no API calls: the event's own copy is used
    settings.SQUARE_LOCATION_ID = "LOC1"


@pytest.fixture
def deferred(monkeypatch):
    """Event IDs handed to the worker."""
    calls = []
    monkeypatch.setattr(
        tasks.process_webhook_event, "defer", lambda **kw: calls.append(kw["event_pk"])
    )
    return calls


def invoice(
    *,
    id="inv:0-1",
    version=1,
    status="UNPAID",
    title="G-1042 team tees",
    total=52500,
    paid=0,
    number="000123",
    location="LOC1",
):
    return {
        "id": id,
        "version": version,
        "location_id": location,
        "order_id": "order-1",
        "invoice_number": number,
        "title": title,
        "status": status,
        "primary_recipient": {"customer_id": "CUST1", "given_name": "Pat", "family_name": "Doe"},
        "payment_requests": [
            {
                "request_type": "BALANCE",
                "due_date": "2026-12-01",
                "computed_amount_money": {"amount": total, "currency": "CAD"},
                "total_completed_amount_money": {"amount": paid, "currency": "CAD"},
            }
        ],
        "public_url": "https://squareup.com/pay-invoice/inv:0-1",
        "updated_at": "2026-11-16T18:00:00Z",
    }


def event(event_type="invoice.published", event_id="ev-1", inv=None):
    inv = inv or invoice()
    return {
        "merchant_id": "M1",
        "type": event_type,
        "event_id": event_id,
        "created_at": "2026-11-16T18:00:01Z",
        "data": {"type": "invoice", "id": inv["id"], "object": {"invoice": inv}},
    }


def sign(body: bytes, url=URL, key=KEY) -> str:
    return base64.b64encode(
        hmac.new(key.encode(), url.encode() + body, hashlib.sha256).digest()
    ).decode()


def deliver(client, payload, signature=None):
    body = json.dumps(payload).encode()
    headers = (
        {} if signature == "" else {"HTTP_X_SQUARE_HMACSHA256_SIGNATURE": signature or sign(body)}
    )
    return client.post(
        reverse("integrations:square_webhook"), body, content_type="application/json", **headers
    )


def run(payload):
    """Store and process one event, as the endpoint and worker would."""
    ev = WebhookEvent.objects.create(
        event_id=payload["event_id"],
        event_type=payload["type"],
        square_object_id=payload["data"]["id"],
        body=payload,
    )
    square.process_event(ev.pk)
    return ev


@pytest.fixture
def job(job_at):
    j = job_at(Status.QUOTE_APPROVED)
    j.job_number = "G-1042"
    j.save()
    return j


# --- The endpoint (INT-01) ----------------------------------------------------------


def test_a_signed_event_is_stored_once_and_queued(
    client, deferred, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        assert deliver(client, event()).status_code == 200
        assert deliver(client, event()).status_code == 200  # Square retried
    assert WebhookEvent.objects.count() == 1
    assert deferred == [WebhookEvent.objects.get().pk]


@pytest.mark.parametrize("signature", ["", "bm90IHRoZSByaWdodCBvbmU="])
def test_a_bad_or_missing_signature_is_refused(client, deferred, signature):
    assert deliver(client, event(), signature=signature).status_code == 403
    assert not WebhookEvent.objects.exists() and deferred == []


def test_a_signature_for_another_address_is_refused(client, deferred):
    payload = event()
    body = json.dumps(payload).encode()
    wrong = sign(body, url="https://jobs.graphnix.com/integrations/square/webhook/")
    assert deliver(client, payload, signature=wrong).status_code == 403


def test_square_off_until_configured(client, settings):
    settings.SQUARE_WEBHOOK_SIGNATURE_KEY = ""
    assert deliver(client, event(), signature="x").status_code == 404


def test_the_endpoint_needs_no_sign_in_and_no_csrf(deferred):
    from django.test import Client

    strict = Client(enforce_csrf_checks=True)
    assert deliver(strict, event()).status_code == 200


# --- Matching (FR-04) ---------------------------------------------------------------


def test_an_invoice_with_the_job_number_in_its_title_links_itself(job):
    run(event())
    inv = SquareInvoice.objects.get()
    assert inv.job == job and inv.match_method == SquareInvoice.Match.TITLE


@pytest.mark.parametrize("title", ["Order g1042", "Tees (G 1042)", "G-1042: 50 tees"])
def test_the_job_number_can_be_written_a_few_ways(job, title):
    run(event(inv=invoice(title=title)))
    assert SquareInvoice.objects.get().job == job


def test_an_invoice_matching_nothing_waits_unlinked(job):
    run(event(inv=invoice(title="Team tees, estimate 1042")))
    inv = SquareInvoice.objects.get()
    assert inv.job is None
    job.refresh_from_db()
    assert job.status == Status.QUOTE_APPROVED


def test_an_invoice_naming_two_jobs_waits_unlinked(job, job_at):
    other = job_at(Status.QUOTING)
    other.job_number = "G-1043"
    other.save()
    run(event(inv=invoice(title="G-1042 and G-1043")))
    assert SquareInvoice.objects.get().job is None


def test_a_link_is_kept_when_the_title_changes(job):
    run(event())
    run(event("invoice.updated", "ev-2", invoice(version=2, title="New title")))
    assert SquareInvoice.objects.get().job == job


def test_another_location_is_ignored(job):
    run(event(inv=invoice(location="OTHER")))
    assert not SquareInvoice.objects.exists()


# --- Payment status (FR-05) ---------------------------------------------------------


def status_of(job):
    job.refresh_from_db()
    return job.payment_status, job.amount_total_cents, job.amount_paid_cents


def test_payment_status_follows_the_invoice(job):
    run(event())
    assert status_of(job) == (PaymentStatus.UNPAID, 52500, 0)
    run(
        event(
            "invoice.payment_made", "ev-2", invoice(version=2, status="PARTIALLY_PAID", paid=20000)
        )
    )
    assert status_of(job) == (PaymentStatus.DEPOSIT, 52500, 20000)
    run(event("invoice.payment_made", "ev-3", invoice(version=3, status="PAID", paid=52500)))
    assert status_of(job) == (PaymentStatus.PAID, 52500, 52500)
    run(event("invoice.refunded", "ev-4", invoice(version=4, status="REFUNDED", paid=52500)))
    assert status_of(job)[0] == PaymentStatus.REFUNDED
    assert SquareInvoice.objects.get().refunded_cents == 52500


def test_a_cancelled_invoice_gives_cancelled(job):
    run(event())
    run(event("invoice.canceled", "ev-2", invoice(version=2, status="CANCELED")))
    assert status_of(job)[0] == PaymentStatus.CANCELLED
    job.refresh_from_db()
    assert job.status == Status.INVOICED  # a cancelled invoice doesn't cancel the job


def test_two_invoices_on_one_job_add_up(job):
    run(event())
    run(
        event(
            event_id="ev-2",
            inv=invoice(id="inv:0-2", number="000124", total=10000, paid=10000, status="PAID"),
        )
    )
    assert status_of(job) == (PaymentStatus.DEPOSIT, 62500, 10000)


def test_an_older_version_never_overwrites_a_newer_one(job):
    run(event("invoice.payment_made", "ev-2", invoice(version=3, status="PAID", paid=52500)))
    run(event("invoice.published", "ev-1", invoice(version=1)))  # arrives late
    assert status_of(job) == (PaymentStatus.PAID, 52500, 52500)
    assert SquareInvoice.objects.get().version == 3


def test_the_same_event_twice_counts_once(job):
    ev = run(event("invoice.payment_made", "ev-2", invoice(status="PAID", paid=52500)))
    square.process_event(ev.pk)
    assert status_of(job)[2] == 52500
    assert AuditLog.objects.filter(action="payment").count() == 1


def test_the_refund_amount_comes_from_the_order(job, settings, monkeypatch):
    settings.SQUARE_ACCESS_TOKEN = "token"
    refunded = invoice(version=2, status="PARTIALLY_REFUNDED", paid=52500)
    monkeypatch.setattr(square, "get_invoice", lambda _id: refunded)
    monkeypatch.setattr(
        square,
        "get_order",
        lambda _id: {"refunds": [{"status": "COMPLETED", "amount_money": {"amount": 5000}}]},
    )
    run(event("invoice.refunded", "ev-2", invoice()))
    assert SquareInvoice.objects.get().refunded_cents == 5000
    assert status_of(job)[0] == PaymentStatus.REFUNDED


def test_the_worker_uses_squares_latest_copy(job, settings, monkeypatch):
    settings.SQUARE_ACCESS_TOKEN = "token"
    monkeypatch.setattr(
        square, "get_invoice", lambda _id: invoice(version=5, status="PAID", paid=52500)
    )
    run(event())  # the event still says UNPAID
    assert status_of(job)[0] == PaymentStatus.PAID


def test_a_deleted_draft_stops_counting(job):
    run(event("invoice.created", "ev-1", invoice(status="DRAFT")))
    deleted = event("invoice.deleted", "ev-2")
    deleted["data"]["object"] = {}
    run(deleted)
    assert SquareInvoice.objects.get().status == "DELETED"
    assert status_of(job)[0] == PaymentStatus.CANCELLED


# --- The move to Invoiced (FR-06) ---------------------------------------------------


def test_a_sent_invoice_moves_the_job_to_invoiced(job):
    run(event())
    job.refresh_from_db()
    assert job.status == Status.INVOICED and job.quote_approved_at is not None
    row = job.history.get(to_status=Status.INVOICED)
    assert row.source == JobStatusHistory.Source.SQUARE and row.changed_by is None
    assert "000123" in row.note
    audit = AuditLog.objects.get(entity_type="jobs.job", action="status_change")
    assert audit.actor is None and audit.changes["square_event"] == [None, "ev-1"]


def test_a_draft_links_but_moves_nothing(job):
    run(event("invoice.created", inv=invoice(status="DRAFT")))
    job.refresh_from_db()
    assert job.status == Status.QUOTE_APPROVED
    assert SquareInvoice.objects.get().job == job


def test_moves_once_however_many_events_arrive(job):
    run(event())
    run(event("invoice.payment_made", "ev-2", invoice(version=2, status="PAID", paid=52500)))
    assert job.history.filter(to_status=Status.INVOICED).count() == 1


def test_a_job_past_invoiced_stays_where_it_is(job):
    job.status = Status.MATERIALS_ORDERED
    job.save()
    run(event())
    job.refresh_from_db()
    assert job.status == Status.MATERIALS_ORDERED


# --- Restarts (NFR-12) --------------------------------------------------------------


def test_a_crash_mid_event_is_finished_on_the_retry_with_no_double_count(job, monkeypatch):
    real_refresh = square.refresh_job

    def crash(*args, **kwargs):
        real_refresh(*args, **kwargs)
        raise RuntimeError("worker stopped")

    monkeypatch.setattr(square, "refresh_job", crash)
    payload = event("invoice.payment_made", "ev-2", invoice(status="PAID", paid=52500))
    ev = WebhookEvent.objects.create(
        event_id="ev-2", event_type=payload["type"], square_object_id="inv:0-1", body=payload
    )
    with pytest.raises(RuntimeError):
        square.process_event(ev.pk)
    ev.refresh_from_db()
    assert ev.processed_at is None and ev.attempts == 1 and "worker stopped" in ev.last_error
    assert status_of(job)[0] == PaymentStatus.UNPAID  # rolled back

    monkeypatch.setattr(square, "refresh_job", real_refresh)
    square.process_event(ev.pk)  # the retry
    ev.refresh_from_db()
    assert ev.processed_at is not None
    assert status_of(job) == (PaymentStatus.PAID, 52500, 52500)
    assert job.history.filter(to_status=Status.INVOICED).count() == 1


# --- Who sees it (NFR-02) -----------------------------------------------------------


def test_payments_are_on_the_timeline_for_money_roles_only(job):
    run(event())
    run(event("invoice.payment_made", "ev-2", invoice(version=2, status="PAID", paid=52500)))
    with_money = [e.title for e in timeline.build(job, show_money=True)]
    assert any(t.startswith("Payment (Square invoice 000123)") for t in with_money)
    assert "Square invoice 000123 linked" in with_money
    without = timeline.build(job)
    assert not any("Payment" in e.title or "$" in e.detail for e in without)
    assert any(e.title == "Quote Approved → Invoiced" for e in without)


def test_production_sees_no_money_on_the_job_page(client, job, make_user):
    run(event())
    run(event("invoice.payment_made", "ev-2", invoice(version=2, status="PAID", paid=52500)))
    client.force_login(make_user("avery", "Avery"))
    for tab in ("items", "timeline", "details"):
        page = client.get(reverse("jobs:detail", args=[job.pk]) + f"?tab={tab}").content.decode()
        assert "$" not in page and "525" not in page and "Paid" not in page


def test_money_formatting():
    assert square.money(52500) == "$525.00"
    assert square.money(123456789) == "$1,234,567.89"
    assert square.money(None) == "—"
    assert square.money(500, "USD") == "$5.00 USD"
