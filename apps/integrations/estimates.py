"""Square estimates, from Square's emails (its API has no estimates).

Square emails the business when an estimate is sent ("A new estimate was sent to Pat Doe
(#000685)") and when the customer accepts it ("You accepted an estimate! (#000685)"). The
tracker reads those, and only those, from the mailbox in ESTIMATE_MAILBOX:

- sent: the job takes the estimate number as its Square number and moves to Quote Sent.
  The job is the one already carrying that number; else the customer's one job waiting at
  Quoting; else, if the customer has none, a new job. Anything less clear waits under
  Invoices for someone to link.
- accepted: the job moves to Invoiced, since Square turns an accepted estimate into an
  invoice at once; that invoice links to the job by customer and total (`job_for_invoice`),
  so no number is needed in its title.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.audit.services import record

from .models import EstimateEmail, SquareEstimate, SquareInvoice

log = logging.getLogger(__name__)

# How close an accepted estimate and its new invoice must be to link by customer and total.
INVOICE_WINDOW = timedelta(hours=48)

# --- Reading an email --------------------------------------------------------------

_SENT = re.compile(r"new estimate was sent to (?P<name>.+?)\s*\(#\s*(?P<number>[\w-]+)\)", re.I)
_ACCEPTED = re.compile(r"accepted an estimate.*?\(#\s*(?P<number>[\w-]+)\)", re.I)
_BODY_NUMBER = re.compile(r"Estimate\s*#\s*(?P<number>[\w-]+)", re.I)
_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_TOTAL = re.compile(r"(?<![A-Za-z])Total\s*:?\s*\$\s*(?P<amount>[\d,]+\.\d{2})")
_AMOUNT = re.compile(r"\$\s*(?P<amount>[\d,]+\.\d{2})")


def _cents(text: str) -> int:
    return int(round(float(text.replace(",", "")) * 100))


def parse(subject: str, text: str) -> dict | None:
    """What a Square estimate email says: kind, number, customer, total. None if it isn't
    one (Square sends other mail from the same addresses)."""
    subject = subject or ""
    text = text or ""
    if m := _SENT.search(subject):
        kind, number, name = EstimateEmail.Kind.SENT, m["number"], m["name"].strip()
    elif (m := _ACCEPTED.search(subject)) or (
        "Estimate Accepted" in text and (m := _BODY_NUMBER.search(text))
    ):
        kind, number, name = EstimateEmail.Kind.ACCEPTED, m["number"], ""
    else:
        return None

    email = ""
    block = re.split(r"(?m)^\s*Customer\s*$", text, maxsplit=1)
    if len(block) == 2:
        after = block[1][:500]
        lines = [line.strip() for line in after.splitlines() if line.strip()]
        if lines and not name and not _EMAIL.search(lines[0]):
            name = lines[0]
        if found := _EMAIL.search(after):
            email = found.group(0)

    totals = _TOTAL.findall(text)
    if totals:
        total = _cents(totals[-1])
    elif amount := _AMOUNT.search(text):
        total = _cents(amount["amount"])
    else:
        total = None
    return {
        "kind": kind,
        "number": number.lstrip("#"),
        "customer_name": name[:255],
        "customer_email": email.lower()[:254],
        "total_cents": total,
    }


# --- Handling one email -------------------------------------------------------------


def handle(message: dict) -> EstimateEmail | None:
    """One message from the mailbox, as Graph lists it (id, subject, receivedDateTime, and
    the body as text). Each message is handled once."""
    if EstimateEmail.objects.filter(message_id=message["id"]).exists():
        return None
    received = parse_datetime(message.get("receivedDateTime") or "") or timezone.now()
    parsed = parse(message.get("subject", ""), (message.get("body") or {}).get("content", ""))
    with transaction.atomic():
        email, created = EstimateEmail.objects.get_or_create(
            message_id=message["id"],
            defaults={
                "received_at": received,
                "kind": parsed["kind"] if parsed else EstimateEmail.Kind.OTHER,
            },
        )
        if not created or parsed is None:
            return email
        if parsed["kind"] == EstimateEmail.Kind.SENT:
            estimate = sent(parsed, at=received)
        else:
            estimate = accepted(parsed, at=received)
        email.estimate = estimate
        email.save(update_fields=["estimate"])
    return email


# --- Sent ---------------------------------------------------------------------------


def _customers(parsed: dict):
    from apps.customers.models import Customer

    if not parsed["customer_email"]:
        return []
    return list(Customer.objects.filter(email__iexact=parsed["customer_email"])[:5])


def _quoting_jobs(parsed: dict) -> list:
    """Jobs waiting at Quoting (with no estimate yet) that the estimate's customer could be
    the customer of: by email first (the customer's, a contact's, or the request's the job
    came from), else by name. Two or more found means it isn't clear which."""
    from django.db.models import Q

    from apps.jobs.models import Job, Status

    waiting = Job.objects.filter(status=Status.QUOTING, square_number="", square_estimate_ref="")
    email = (parsed["customer_email"] or "").strip()
    if email:
        found = list(
            waiting.filter(
                Q(customer__email__iexact=email)
                | Q(customer__contacts__email__iexact=email)
                | Q(inquiry__from_email__iexact=email)
            ).distinct()[:2]
        )
        if found:
            return found
    name = " ".join((parsed["customer_name"] or "").split())
    if not name:
        return []
    return list(
        waiting.filter(
            Q(customer__display_name__iexact=name)
            | Q(customer__company_name__iexact=name)
            | Q(customer__contacts__full_name__iexact=name)
            | Q(inquiry__from_name__iexact=name)
        ).distinct()[:2]
    )


def title_for(name: str, number: str) -> str:
    """A job made for an estimate is named for its customer ("000685 · Pat Doe" on the
    board); the estimate number only when Square gave no name."""
    return (" ".join((name or "").split()) or f"Estimate {number}")[:200]


def _job_for(estimate: SquareEstimate, parsed: dict):
    """(job, how) for a newly sent estimate, or (None, "") when it should wait."""
    from apps.customers.models import Customer
    from apps.jobs import services
    from apps.jobs.models import Job

    number = estimate.number
    by_number = list(
        Job.objects.filter(square_number=number) | Job.objects.filter(square_estimate_ref=number)
    )
    if len(by_number) == 1:
        return by_number[0], SquareEstimate.Match.NUMBER

    customers = _customers(parsed)
    quoting = _quoting_jobs(parsed)
    if len(quoting) == 1:
        return quoting[0], SquareEstimate.Match.CUSTOMER
    if quoting or len(customers) > 1:
        return None, ""

    if customers:
        customer = customers[0]
    elif parsed["customer_email"] or parsed["customer_name"]:
        customer = Customer.objects.create(
            display_name=(parsed["customer_name"] or parsed["customer_email"])[:200],
            email=parsed["customer_email"],
        )
        record(None, customer, "create", {"source": [None, f"Square estimate {number}"]})
    else:
        return None, ""
    job = services.square_estimate_job(
        customer=customer,
        title=title_for(parsed["customer_name"] or customer.display_name, number),
        note=f"Square estimate {number} sent",
    )
    return job, SquareEstimate.Match.CREATED


def attach(estimate: SquareEstimate, job, *, how: str, user=None) -> None:
    """Put the estimate on the job: its number becomes the job's Square number, and a job
    still at Quoting moves to Quote Sent."""
    from apps.jobs import services
    from apps.jobs.models import Job

    estimate.job = job
    estimate.match_method = how
    estimate.linked_by = user
    estimate.not_a_job_at = None
    estimate.not_a_job_by = None
    estimate.save()
    fields = {}
    if not job.square_estimate_ref:
        fields["square_estimate_ref"] = estimate.number
    taken = Job.objects.filter(square_number=estimate.number).exclude(pk=job.pk).exists()
    if not job.square_number and not taken:
        fields["square_number"] = estimate.number
    if fields:
        Job.objects.filter(pk=job.pk).update(**fields)
        record(user, job, "update", {k: [None, v] for k, v in fields.items()})
        job.refresh_from_db()
    record(user, estimate, "link", {"estimate": [None, estimate.number]}, job=job)
    if estimate.sent_at:
        services.square_quote_sent(job, note=f"Square estimate {estimate.number} sent")
    if estimate.accepted_at:
        services.square_estimate_accepted(job, note=f"Square estimate {estimate.number} accepted")
        link_invoice(estimate)


def sent(parsed: dict, *, at) -> SquareEstimate:
    estimate = SquareEstimate.objects.select_for_update().filter(number=parsed["number"]).first()
    estimate = estimate or SquareEstimate(number=parsed["number"])
    for field in ("customer_name", "customer_email", "total_cents"):
        if parsed[field] not in ("", None):
            setattr(estimate, field, parsed[field])
    estimate.sent_at = estimate.sent_at or at
    estimate.save()
    if estimate.job_id is None and estimate.not_a_job_at is None:
        job, how = _job_for(estimate, parsed)
        if job is not None:
            attach(estimate, job, how=how)
    return estimate


# --- Accepted -----------------------------------------------------------------------


def accepted(parsed: dict, *, at) -> SquareEstimate:
    from apps.jobs import services

    estimate = SquareEstimate.objects.select_for_update().filter(number=parsed["number"]).first()
    if estimate is None:
        # The "sent" email was missed: treat this one as both.
        estimate = sent(parsed, at=at)
    estimate.accepted_at = estimate.accepted_at or at
    if estimate.total_cents is None:
        estimate.total_cents = parsed["total_cents"]
    estimate.save()
    if estimate.job_id:
        services.square_estimate_accepted(
            estimate.job, note=f"Square estimate {estimate.number} accepted"
        )
        link_invoice(estimate)
    return estimate


# --- The invoice Square makes from an accepted estimate -----------------------------


def _same_customer(estimate: SquareEstimate, *, square_customer_id: str, email: str, name: str):
    job_customer = estimate.job.customer
    if square_customer_id and job_customer.square_customer_id == square_customer_id:
        return True
    if email and email.lower() in (estimate.customer_email, (job_customer.email or "").lower()):
        return True
    return bool(name) and name.casefold() == estimate.customer_name.casefold()


def job_for_invoice(data: dict):
    """The job of the accepted estimate this new invoice came from: accepted within
    INVOICE_WINDOW, same total, same customer, and only one such estimate."""
    from . import square

    fields = square.read_invoice(data)
    if fields["total_cents"] is None:
        return None
    created = parse_datetime(data.get("created_at") or "") or timezone.now()
    recipient = data.get("primary_recipient") or {}
    candidates = [
        e
        for e in SquareEstimate.objects.filter(
            job__isnull=False,
            total_cents=fields["total_cents"],
            accepted_at__gte=created - INVOICE_WINDOW,
            accepted_at__lte=created + INVOICE_WINDOW,
        ).select_related("job__customer")
        if _same_customer(
            e,
            square_customer_id=fields["square_customer_id"],
            email=recipient.get("email_address") or "",
            name=fields["recipient_name"],
        )
    ]
    jobs = {e.job_id: e.job for e in candidates}
    return next(iter(jobs.values())) if len(jobs) == 1 else None


def link_invoice(estimate: SquareEstimate) -> SquareInvoice | None:
    """When the invoice arrived before the "accepted" email: find it among the invoices
    waiting to be linked and link it."""
    from . import linking, square

    if not estimate.job_id or estimate.total_cents is None or not estimate.accepted_at:
        return None
    around = (estimate.accepted_at - INVOICE_WINDOW, estimate.accepted_at + INVOICE_WINDOW)
    matches = [
        inv
        for inv in linking.waiting().filter(
            total_cents=estimate.total_cents, created_at__range=around
        )
        if not inv.match_method
        and _same_customer(
            estimate,
            square_customer_id=inv.square_customer_id,
            email="",
            name=inv.recipient_name,
        )
    ]
    if len(matches) != 1:
        return None
    invoice = SquareInvoice.objects.select_for_update().get(pk=matches[0].pk)
    invoice.job = estimate.job
    invoice.match_method = SquareInvoice.Match.ESTIMATE
    invoice.linked_at = timezone.now()
    invoice.save()
    record(None, invoice, "link", {"invoice": [None, invoice.invoice_number]}, job=invoice.job)
    square.match_customer(invoice.job.customer, invoice.square_customer_id, how="invoice")
    square.refresh_job(invoice.job, invoice=invoice)
    return invoice


# --- Reading the mailbox ------------------------------------------------------------

RESULT_KEY = "estimate_mail"
OVERLAP = timedelta(days=1)


def enabled() -> bool:
    return bool(settings.ESTIMATE_MAILBOX and settings.M365_CONFIGURED)


def check_mail(*, since=None) -> dict:
    """Read Square's estimate emails received since `since` (default: a day before the last
    one read) and handle each new one. The result is kept for the health panel."""
    from apps.accounts.models import AppSetting, get_setting
    from apps.m365 import graph

    now = timezone.now()
    result = {"at": now.isoformat(), "read": 0, "error": ""}
    last = get_setting(RESULT_KEY) or {}
    if since is None:
        newest = parse_datetime(last.get("newest") or "")
        since = (newest - OVERLAP) if newest else now - OVERLAP
    result["newest"] = last.get("newest", "")
    try:
        for message in graph.list_messages(
            settings.ESTIMATE_MAILBOX, senders=settings.ESTIMATE_SENDERS, since=since
        ):
            try:
                email = handle(message)
            except Exception:
                # Left unrecorded, so the next check tries it again; the log sends the
                # crash email.
                log.exception("Square estimate email %s couldn't be handled", message.get("id"))
                result["error"] = "An estimate email couldn't be handled; see the crash email."
                continue
            if email is not None:
                result["read"] += 1
            received = message.get("receivedDateTime") or ""
            if received > result["newest"]:
                result["newest"] = received
    except graph.GraphError as exc:
        result["error"] = str(exc)[:500]
        log.error("Reading estimate emails failed: %s", exc)
    AppSetting.objects.update_or_create(key=RESULT_KEY, defaults={"value": result})
    return result


def preview(*, since=None) -> list[dict]:
    """What check_mail would do, without keeping any of it: each estimate email goes through
    the real matching inside a transaction that is rolled back at the end. Nothing is
    saved, not even "last read". (Job numbers taken from the G- sequence stay used: a few
    G- numbers are skipped, which is harmless.)"""
    from apps.customers.models import Customer
    from apps.m365 import graph

    since = since or timezone.now() - OVERLAP
    rows = []
    with transaction.atomic():
        for message in graph.list_messages(
            settings.ESTIMATE_MAILBOX, senders=settings.ESTIMATE_SENDERS, since=since
        ):
            row = {"received": (message.get("receivedDateTime") or "")[:16].replace("T", " ")}
            if EstimateEmail.objects.filter(message_id=message.get("id")).exists():
                rows.append({**row, "number": "", "kind": "", "result": "already read"})
                continue
            customers = Customer.objects.count()
            parsed = parse(
                message.get("subject", ""), (message.get("body") or {}).get("content", "")
            )
            # On a job already: before this run, or from an earlier email in this preview.
            known = (
                bool(parsed)
                and SquareEstimate.objects.filter(
                    number=parsed["number"], job__isnull=False
                ).exists()
            )
            try:
                with transaction.atomic():
                    email = handle(message)
            except Exception as exc:  # shown, not raised: the preview goes on
                rows.append({**row, "number": "", "kind": "", "result": f"error: {exc}"[:200]})
                continue
            estimate = email.estimate if email is not None else None
            if estimate is None:
                rows.append({**row, "number": "", "kind": "", "result": "not an estimate email"})
                continue
            estimate.refresh_from_db()
            job = estimate.job
            if job is None:
                result = "waits under Invoices › Estimates"
            else:
                job.refresh_from_db()
                status = job.get_status_display()
                if known:
                    result = f"its job moves to {status}"
                elif estimate.match_method == SquareEstimate.Match.CREATED:
                    new = " and a new customer" if Customer.objects.count() > customers else ""
                    result = f"new job{new} at {status}"
                else:
                    # Named as staff know it now: its G- number (the estimate number
                    # replaces it once this runs for real).
                    result = (
                        f"existing job {job.job_number} {job.title} "
                        f"({job.customer.display_name}) now at {status}"
                    )
            rows.append({**row, "number": estimate.number, "kind": email.kind, "result": result})
        transaction.set_rollback(True)
    return rows


def last_result() -> dict | None:
    from apps.accounts.models import get_setting

    return get_setting(RESULT_KEY)


# --- By hand: the Estimates list ------------------------------------------------------


def waiting():
    return SquareEstimate.objects.filter(job__isnull=True, not_a_job_at__isnull=True)


@transaction.atomic
def link(estimate: SquareEstimate, job, *, user) -> SquareEstimate:
    from django.core.exceptions import PermissionDenied

    from .linking import PERMISSION

    if not user.has_perm(PERMISSION):
        raise PermissionDenied("You can't link Square estimates.")
    estimate = SquareEstimate.objects.select_for_update().get(pk=estimate.pk)
    if estimate.job_id:
        raise PermissionDenied("This estimate is already on a job.")
    attach(estimate, job, how=SquareEstimate.Match.MANUAL, user=user)
    return estimate


@transaction.atomic
def create_job(estimate: SquareEstimate, *, user, customer, title: str):
    from django.core.exceptions import PermissionDenied

    from apps.jobs import services

    from .linking import PERMISSION

    if not (user.has_perm(PERMISSION) and user.has_perm("jobs.add_job")):
        raise PermissionDenied("You can't create jobs from estimates.")
    estimate = SquareEstimate.objects.select_for_update().get(pk=estimate.pk)
    if estimate.job_id:
        raise PermissionDenied("This estimate is already on a job.")
    job = services.create_job(
        customer=customer,
        title=(title or title_for(estimate.customer_name, estimate.number))[:200],
        start=services.Status.QUOTING,
        user=user,
    )
    attach(estimate, job, how=SquareEstimate.Match.MANUAL, user=user)
    return job


@transaction.atomic
def set_aside(estimate: SquareEstimate, *, user) -> None:
    from django.core.exceptions import PermissionDenied

    from .linking import PERMISSION

    if not user.has_perm(PERMISSION):
        raise PermissionDenied("You can't change Square estimates.")
    estimate = SquareEstimate.objects.select_for_update().get(pk=estimate.pk)
    estimate.not_a_job_by = user
    estimate.not_a_job_at = timezone.now()
    estimate.save(update_fields=["not_a_job_by", "not_a_job_at", "updated_at"])
    record(user, estimate, "not_a_job", {"estimate": [None, estimate.number]})
