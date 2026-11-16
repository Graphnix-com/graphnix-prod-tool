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
    pass


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


def _get(path: str) -> dict | None:
    base = API_BASE.get(settings.SQUARE_ENVIRONMENT, API_BASE["sandbox"])
    try:
        response = requests.get(
            base + path,
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
    if not response.ok:
        raise SquareError(f"Square answered {response.status_code} for {path}")
    return response.json()


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


@transaction.atomic
def apply(data: dict, *, refunded: int | None = None, event_id: str = "") -> SquareInvoice | None:
    """Store the latest copy of a Square invoice, link it to its job, and bring the job's
    payment status and stage up to date. An older version never overwrites a newer one."""
    location = settings.SQUARE_LOCATION_ID
    if location and data.get("location_id") and data["location_id"] != location:
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
    if invoice.job_id is None and invoice.not_a_job_at is None:
        job = find_job(data)
        if job is not None:
            invoice.job = job
            invoice.match_method = SquareInvoice.Match.TITLE
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


def refresh_job(job: Job, *, invoice: SquareInvoice | None = None, event_id: str = "") -> Job:
    """Recalculate a job's money from its invoices; move it to Invoiced once one is sent."""
    job = Job.objects.select_for_update().get(pk=job.pk)
    invoices = list(job.square_invoices.all())
    status, total, paid, currency = payment_summary(invoices)
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
        with transaction.atomic():
            event = WebhookEvent.objects.select_for_update().get(pk=event_pk)
            if event.processed_at is not None:
                return
            if event.event_type == "invoice.deleted":
                mark_deleted(event.square_object_id, event_id=event.event_id)
            elif latest is not None:
                apply(latest, refunded=refunded, event_id=event.event_id)
            event.processed_at = timezone.now()
            event.attempts += 1
            event.last_error = ""
            event.save(update_fields=["processed_at", "attempts", "last_error"])
    except Exception as exc:
        WebhookEvent.objects.filter(pk=event_pk).update(
            attempts=event.attempts + 1, last_error=str(exc)[:2000]
        )
        raise


def _latest_invoice(event: WebhookEvent) -> tuple[dict | None, int | None]:
    """The invoice as Square has it now (falling back to the event's copy), and the
    refunded amount if it has refunds. Network calls happen here, outside any lock."""
    if not event.event_type.startswith("invoice.") or event.event_type == "invoice.deleted":
        return None, None
    data = (event.body.get("data") or {}).get("object") or {}
    invoice = data.get("invoice")
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
