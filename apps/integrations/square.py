"""Square, read-only: webhook signatures, the API client, and applying an invoice to its job.

How an invoice reaches its job (Step 2 spec, answers to questions 2 and 3):
- Square gives the invoice a new number, not the estimate's, so the link is the tracker's
  job number (G-1042) written in the invoice title or message.
- Anything that matches no job, or more than one, waits in the "Link to job" inbox.
- A link, once made, is kept, even if the title changes later.

Job amounts and payment status are recalculated from all of the job's invoices in
`refresh_job`, in the same transaction as the invoice copy, so a repeat event never
double-counts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
from datetime import date

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.audit.services import record
from apps.jobs import services as job_services
from apps.jobs.models import Job, PaymentStatus

from .models import SquareInvoice, WebhookEvent

log = logging.getLogger(__name__)

API_BASE = {
    "production": "https://connect.squareup.com",
    "sandbox": "https://connect.squareupsandbox.com",
}
TIMEOUT_SECONDS = 15

# Square invoice statuses.
CANCELLED_STATES = {"CANCELED", "FAILED", "DELETED"}
REFUND_STATES = {"REFUNDED", "PARTIALLY_REFUNDED"}

# G-1042, g1042, G 1042: the tracker's job number, not part of a longer word or number.
_JOB_NUMBER = re.compile(r"(?<![A-Za-z0-9])G-?\s?(\d{3,})(?!\d)", re.IGNORECASE)


class SquareError(RuntimeError):
    """Square didn't answer, or answered with an error. Worth retrying."""


class SquareAccessError(SquareError):
    """Square refused the token (401/403): a setup problem that retrying won't fix."""


# --- Webhook signature -------------------------------------------------------------


def valid_signature(body: bytes, signature: str, notification_url: str) -> bool:
    """Square signs HMAC-SHA256(signature key, notification URL + raw body), base64."""
    key = settings.SQUARE_WEBHOOK_SIGNATURE_KEY
    if not key or not signature:
        return False
    digest = hmac.new(key.encode(), notification_url.encode() + body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature.strip())


# --- API client (GET only) ---------------------------------------------------------


def api_configured() -> bool:
    return bool(settings.SQUARE_ACCESS_TOKEN)


def _get(path: str, params: dict | None = None) -> dict | None:
    base = API_BASE.get(settings.SQUARE_ENVIRONMENT, API_BASE["sandbox"])
    try:
        response = requests.get(
            base + path,
            params=params,
            headers={
                "Authorization": f"Bearer {settings.SQUARE_ACCESS_TOKEN}",
                "Square-Version": settings.SQUARE_API_VERSION,
                "Accept": "application/json",
            },
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise SquareError(f"Square didn't answer: {exc.__class__.__name__}") from exc
    if response.status_code == 404:
        return None
    if response.status_code in (401, 403):
        raise SquareAccessError(f"Square refused the access token for {path}: {_detail(response)}")
    if not response.ok:
        raise SquareError(f"Square answered {response.status_code} for {path}")
    return response.json()


def _detail(response) -> str:
    try:
        errors = response.json().get("errors") or [{}]
        return errors[0].get("detail") or errors[0].get("code") or str(response.status_code)
    except ValueError:
        return str(response.status_code)


def _post(path: str, body: dict) -> dict | None:
    """A read that Square takes as a POST (searches). Nothing is changed in Square."""
    base = API_BASE.get(settings.SQUARE_ENVIRONMENT, API_BASE["sandbox"])
    try:
        response = requests.post(
            base + path,
            json=body,
            headers={
                "Authorization": f"Bearer {settings.SQUARE_ACCESS_TOKEN}",
                "Square-Version": settings.SQUARE_API_VERSION,
                "Accept": "application/json",
            },
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise SquareError(f"Square didn't answer: {exc.__class__.__name__}") from exc
    if response.status_code in (401, 403):
        raise SquareAccessError(f"Square refused the access token for {path}: {_detail(response)}")
    if not response.ok:
        raise SquareError(f"Square answered {response.status_code} for {path}")
    return response.json()


def search_orders(
    location_id: str, *, updated_since: str, max_pages: int = 50, states: list | None = None
):
    """Orders at a location updated since an RFC 3339 time, newest first. `states` adds
    Square's state filter (drafts are only returned when asked for)."""
    cursor = None
    for _ in range(max_pages):
        query_filter = {"date_time_filter": {"updated_at": {"start_at": updated_since}}}
        if states:
            query_filter["state_filter"] = {"states": states}
        body = {
            "location_ids": [location_id],
            "limit": 500,
            "query": {
                "filter": query_filter,
                "sort": {"sort_field": "UPDATED_AT", "sort_order": "DESC"},
            },
        }
        if cursor:
            body["cursor"] = cursor
        page = _post("/v2/orders/search", body) or {}
        yield from page.get("orders", [])
        cursor = page.get("cursor")
        if not cursor:
            return


def search_customers(*, updated_since: str, max_pages: int = 5):
    """Customers updated since an RFC 3339 time."""
    cursor = None
    for _ in range(max_pages):
        body = {"limit": 100, "query": {"filter": {"updated_at": {"start_at": updated_since}}}}
        if cursor:
            body["cursor"] = cursor
        page = _post("/v2/customers/search", body) or {}
        yield from page.get("customers", [])
        cursor = page.get("cursor")
        if not cursor:
            return


def list_locations() -> list[str]:
    return [loc["id"] for loc in (_get("/v2/locations") or {}).get("locations", [])]


def get_customer(customer_id: str) -> dict | None:
    return (_get(f"/v2/customers/{customer_id}") or {}).get("customer")


def list_invoices(location_id: str, max_pages: int = 50):
    """Every invoice at a location, newest pages first as Square orders them."""
    cursor = None
    for _ in range(max_pages):
        params = {"location_id": location_id, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        body = _get("/v2/invoices", params) or {}
        yield from body.get("invoices", [])
        cursor = body.get("cursor")
        if not cursor:
            return


def get_invoice(invoice_id: str) -> dict | None:
    body = _get(f"/v2/invoices/{invoice_id}")
    return (body or {}).get("invoice")


def get_order(order_id: str) -> dict | None:
    body = _get(f"/v2/orders/{order_id}")
    return (body or {}).get("order")


def refunded_cents(order: dict | None) -> int | None:
    """Money returned on an invoice's order, or None if the order doesn't say."""
    if not order:
        return None
    refunds = order.get("refunds")
    if refunds is not None:
        return sum(
            _cents(r.get("amount_money")) or 0 for r in refunds if r.get("status") == "COMPLETED"
        )
    returned = (order.get("return_amounts") or {}).get("total_money")
    return _cents(returned)


# --- Reading an invoice ------------------------------------------------------------


def _cents(money: dict | None) -> int | None:
    if not money or money.get("amount") is None:
        return None
    return int(money["amount"])


def _currency(*moneys) -> str | None:
    for money in moneys:
        if money and money.get("currency"):
            return money["currency"]
    return None


def _recipient_name(recipient: dict) -> str:
    if recipient.get("company_name"):
        return recipient["company_name"]
    return " ".join(p for p in (recipient.get("given_name"), recipient.get("family_name")) if p)


def read_invoice(data: dict, refunded: int | None = None) -> dict:
    """The fields we keep, from a Square invoice object."""
    requests_ = data.get("payment_requests") or []
    computed = [_cents(r.get("computed_amount_money")) for r in requests_]
    paid = [_cents(r.get("total_completed_amount_money")) for r in requests_]
    due = sorted(r["due_date"] for r in requests_ if r.get("due_date"))
    status = str(data.get("status") or "")
    if refunded is None:
        # Without the order, a full refund is all of what was paid; a partial one is unknown.
        refunded = sum(p or 0 for p in paid) if status == "REFUNDED" else 0
    currency = _currency(
        *(r.get("computed_amount_money") for r in requests_),
        *(r.get("total_completed_amount_money") for r in requests_),
    )
    recipient = data.get("primary_recipient") or {}
    return {
        "version": int(data.get("version") or 0),
        "location_id": data.get("location_id") or "",
        "order_id": data.get("order_id") or "",
        "invoice_number": data.get("invoice_number") or "",
        "title": (data.get("title") or "")[:255],
        "status": status,
        "square_customer_id": recipient.get("customer_id") or "",
        "recipient_name": _recipient_name(recipient)[:255],
        "currency": currency or "CAD",
        "total_cents": sum(c for c in computed if c is not None)
        if any(c is not None for c in computed)
        else None,
        "paid_cents": sum(p or 0 for p in paid),
        "refunded_cents": refunded,
        "due_date": date.fromisoformat(due[-1]) if due else None,
        "public_url": (data.get("public_url") or "")[:500],
        "square_updated_at": parse_datetime(data["updated_at"]) if data.get("updated_at") else None,
    }


def find_job(data: dict) -> Job | None:
    """The one job whose G- number is in the invoice's title or message."""
    text = " ".join(str(data.get(k) or "") for k in ("title", "description"))
    numbers = {f"G-{n}" for n in _JOB_NUMBER.findall(text)}
    jobs = list(Job.objects.filter(job_number__in=numbers)[:2]) if numbers else []
    return jobs[0] if len(jobs) == 1 else None


# --- Applying an invoice -----------------------------------------------------------


def money(cents: int | None, currency: str = "CAD") -> str:
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    text = f"{sign}${cents // 100:,}.{cents % 100:02d}"
    return text if currency == "CAD" else f"{text} {currency}"


def other_location(data: dict) -> str:
    """The invoice's location, if it isn't the one the tracker reads; else ''."""
    location = settings.SQUARE_LOCATION_ID
    theirs = data.get("location_id") or ""
    return theirs if location and theirs and theirs != location else ""


def match_customer(customer, square_customer_id: str, *, how: str) -> bool:
    """Give a tracker customer its Square customer ID (FR-08), if it has none yet and no
    other tracker customer has that ID. Never changes an existing link."""
    if not square_customer_id or customer.square_customer_id:
        return False
    if type(customer).objects.filter(square_customer_id=square_customer_id).exists():
        return False
    customer.square_customer_id = square_customer_id
    customer.save(update_fields=["square_customer_id"])
    record(
        None,
        customer,
        "update",
        {"square_customer_id": [None, square_customer_id], "matched_by": [None, how]},
    )
    return True


@transaction.atomic
def apply(data: dict, *, refunded: int | None = None, event_id: str = "") -> SquareInvoice | None:
    """Store the latest copy of a Square invoice, link it to its job, and bring the job's
    payment status and stage up to date. An older version never overwrites a newer one."""
    if other_location(data):
        return None
    square_id = data["id"]
    invoice = SquareInvoice.objects.select_for_update().filter(square_id=square_id).first()
    fields = read_invoice(data, refunded)
    if invoice is not None and fields["version"] < invoice.version:
        log.info("Square invoice %s: version %s is older than stored", square_id, fields["version"])
        return invoice

    if invoice is None:
        invoice = SquareInvoice(square_id=square_id)
    for name, value in fields.items():
        setattr(invoice, name, value)
    linked_now = False
    if invoice.job_id is None and invoice.not_a_job_at is None and not invoice.match_method:
        job, how = find_job(data), SquareInvoice.Match.TITLE
        if job is None:
            from . import estimates

            job, how = estimates.job_for_invoice(data), SquareInvoice.Match.ESTIMATE
        if job is not None:
            invoice.job = job
            invoice.match_method = how
            invoice.linked_at = timezone.now()
            linked_now = True
    invoice.save()

    if linked_now:
        record(
            None,
            invoice,
            "link",
            {"invoice": [None, invoice.invoice_number], "square_event": [None, event_id]},
            job=invoice.job,
        )
    if invoice.job_id:
        match_customer(invoice.job.customer, invoice.square_customer_id, how="invoice")
        refresh_job(invoice.job, invoice=invoice, event_id=event_id)
    return invoice


def mark_deleted(square_id: str, *, event_id: str = "") -> None:
    """A draft invoice deleted in Square."""
    with transaction.atomic():
        invoice = SquareInvoice.objects.select_for_update().filter(square_id=square_id).first()
        if invoice is None:
            return
        invoice.status = "DELETED"
        invoice.save(update_fields=["status", "updated_at"])
        if invoice.job_id:
            refresh_job(invoice.job, invoice=invoice, event_id=event_id)


def payment_summary(invoices) -> tuple[str, int | None, int, str]:
    """(payment status, total, paid, currency) for a job, from all its invoices."""
    invoices = list(invoices)
    if not invoices:
        return PaymentStatus.UNPAID, None, 0, "CAD"
    active = [i for i in invoices if i.status not in CANCELLED_STATES]
    if not active:
        return PaymentStatus.CANCELLED, None, 0, invoices[0].currency
    totals = [i.total_cents for i in active if i.total_cents is not None]
    total = sum(totals) if totals else None
    paid = sum(i.paid_cents for i in active)
    refunded = sum(i.refunded_cents for i in active)
    if refunded > 0 or any(i.status in REFUND_STATES for i in active):
        status = PaymentStatus.REFUNDED
    elif total and paid >= total:
        status = PaymentStatus.PAID
    elif paid > 0:
        status = PaymentStatus.DEPOSIT
    else:
        status = PaymentStatus.UNPAID
    return status, total, paid, active[0].currency


def refresh_job(
    job: Job, *, invoice: SquareInvoice | None = None, order=None, event_id: str = ""
) -> Job:
    """Recalculate a job's money from its invoices and store orders; move it to Invoiced
    once an invoice is sent."""
    job = Job.objects.select_for_update().get(pk=job.pk)
    invoices = list(job.square_invoices.all())
    status, total, paid, currency = payment_summary(invoices + list(job.square_orders.all()))
    before = (job.payment_status, job.amount_total_cents, job.amount_paid_cents, job.currency)
    after = (status, total, paid, currency)
    if before != after:
        job.payment_status, job.amount_total_cents, job.amount_paid_cents, job.currency = after
        job.save(
            update_fields=[
                "payment_status",
                "amount_total_cents",
                "amount_paid_cents",
                "currency",
                "updated_at",
            ]
        )
        changes = {}
        if before[0] != status:
            changes["payment"] = [
                PaymentStatus(before[0]).label,
                PaymentStatus(status).label,
            ]
        if before[2] != paid:
            changes["paid"] = [money(before[2], before[3]), money(paid, currency)]
        if before[1] != total:
            changes["total"] = [money(before[1], before[3]), money(total, currency)]
        if invoice is not None:
            changes["invoice"] = [invoice.invoice_number, invoice.invoice_number]
        if order is not None:
            changes["order"] = [order.number, order.number]
        changes["square_event"] = [None, event_id]
        record(None, invoice or job, "payment", changes, job=job)

    sent = [i for i in invoices if i.sent and i.status not in CANCELLED_STATES]
    if sent:
        number = (invoice if invoice in sent else sent[0]).invoice_number
        job_services.square_invoiced(
            job, note=f"Square invoice {number} sent".replace("  ", " "), event_id=event_id
        )
    return job


# --- Processing a stored webhook event ---------------------------------------------


def process_event(event_pk: int) -> None:
    """Apply one stored event. Safe to run again: a processed event is skipped, and
    everything it changes is recalculated, not added to."""
    event = WebhookEvent.objects.get(pk=event_pk)
    if event.processed_at is not None:
        return
    try:
        latest, refunded = _latest_invoice(event)
        order, buyer = (None, None)
        if event.event_type.startswith("order.") and api_configured() and event.square_object_id:
            from . import orders

            order, buyer = orders.fetch(event.square_object_id)
        with transaction.atomic():
            event = WebhookEvent.objects.select_for_update().get(pk=event_pk)
            if event.processed_at is not None:
                return
            note = ""
            if event.event_type == "invoice.deleted":
                mark_deleted(event.square_object_id, event_id=event.event_id)
            elif event.event_type.startswith("customer."):
                customer_event(event.body)
            elif event.event_type.startswith("order."):
                note = _order_event(order, buyer, event.event_id)
            elif latest is not None and other_location(latest):
                note = (
                    f"Skipped: the invoice is from location {other_location(latest)}; the "
                    f"tracker reads only {settings.SQUARE_LOCATION_ID} (SQUARE_LOCATION_ID)."
                )
            elif latest is not None:
                apply(latest, refunded=refunded, event_id=event.event_id)
            event.processed_at = timezone.now()
            event.attempts += 1
            event.last_error = note
            event.save(update_fields=["processed_at", "attempts", "last_error"])
    except SquareAccessError as exc:
        # A setup problem: retrying won't help. Left unprocessed for the health panel; the
        # nightly check tries again once the token or location is fixed.
        log.error("Square event %s: %s", event.event_id, exc)
        WebhookEvent.objects.filter(pk=event_pk).update(
            attempts=event.attempts + 1, last_error=str(exc)[:2000]
        )
    except Exception as exc:
        WebhookEvent.objects.filter(pk=event_pk).update(
            attempts=event.attempts + 1, last_error=str(exc)[:2000]
        )
        raise


def _order_event(order: dict | None, buyer: dict | None, event_id: str) -> str:
    """order.created / order.updated: store orders become jobs; the rest are left alone.
    Returns a note for the event log."""
    from . import orders

    if order is None:
        return "Skipped: Square has no such order, or Square isn't set up here."
    if not orders.is_store_order(order):
        return ""  # a counter sale or an invoice's order: normal, nothing to do
    if other_location(order):
        return f"Skipped: the order is from location {other_location(order)}."
    orders.apply(order, square_customer=buyer, event_id=event_id)
    return ""


def customer_event(body: dict) -> None:
    """customer.created / customer.updated: a tracker customer with the same email and no
    Square ID yet gets this one (FR-08). Nothing else is copied from Square."""
    data = (body.get("data") or {}).get("object") or {}
    sq = data.get("customer") or {}
    square_id = sq.get("id") or (body.get("data") or {}).get("id") or ""
    email = (sq.get("email_address") or "").strip()
    if not square_id or not email:
        return
    from apps.customers.models import Customer

    if Customer.objects.filter(square_customer_id=square_id).exists():
        return
    candidates = list(Customer.objects.filter(email__iexact=email, square_customer_id="")[:2])
    if len(candidates) == 1:
        match_customer(candidates[0], square_id, how="email")


def _latest_invoice(event: WebhookEvent) -> tuple[dict | None, int | None]:
    """The invoice as Square has it now (falling back to the event's copy), and the
    refunded amount if it has refunds. Network calls happen here, outside any lock."""
    if not event.event_type.startswith("invoice.") or event.event_type == "invoice.deleted":
        return None, None
    data = (event.body.get("data") or {}).get("object") or {}
    invoice = data.get("invoice")
    if invoice and other_location(invoice):
        return invoice, None  # not ours: don't ask Square, which would refuse anyway
    if api_configured() and event.square_object_id:
        invoice = get_invoice(event.square_object_id) or invoice
    refunded = None
    if (
        invoice
        and api_configured()
        and invoice.get("status") in REFUND_STATES
        and invoice.get("order_id")
    ):
        refunded = refunded_cents(get_order(invoice["order_id"]))
    return invoice, refunded
