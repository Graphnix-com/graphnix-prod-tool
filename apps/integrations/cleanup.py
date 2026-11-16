"""Staging only: remove what came from Square, so real customers' details don't stay on a test
server once testing is done.

Removed:
- customers linked to a Square customer, or made by the tracker from Square (an import, a
  store order, an estimate email, an invoice): their contacts, addresses and jobs go too;
- jobs the tracker made for a store order or an estimate;
- every stored Square invoice, store order, estimate and estimate email;
- the audit entries about all of those, since they repeat names and emails.

Kept: staff's own customers and jobs. A kept job that was linked to an invoice or an estimate
is unlinked, its payment recalculated (to none) and its estimate number cleared. Its status
stays where Square moved it.

Square itself is never touched. The next nightly check reads the sandbox again (last 3 days);
estimate emails from the last day are read again on the next mail check.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Q

from apps.audit.models import AuditLog

from .models import EstimateEmail, SquareEstimate, SquareInvoice, SquareOrder

# How the tracker labels customers it made from Square, in their audit "create" entry.
_MADE_FROM_SQUARE = ("Square", "Online store order")
_SQUARE_TYPES = [
    "integrations.squareinvoice",
    "integrations.squareorder",
    "integrations.squareestimate",
    "integrations.estimateemail",
]


def _made_customer_ids() -> set[int]:
    rows = AuditLog.objects.filter(
        entity_type="customers.customer", action="create", changes__has_key="source"
    ).values_list("entity_id", "changes")
    ids = set()
    for entity_id, changes in rows:
        source = (changes or {}).get("source") or [None, ""]
        if str(source[-1] or "").startswith(_MADE_FROM_SQUARE):
            ids.add(entity_id)
    return ids


def plan() -> dict:
    """What `clear` would remove, as querysets."""
    from apps.customers.models import Customer
    from apps.jobs.models import Job

    customers = Customer.objects.filter(Q(pk__in=_made_customer_ids()) | ~Q(square_customer_id=""))
    jobs = Job.objects.filter(
        Q(customer__in=customers)
        | Q(square_orders__isnull=False)
        | Q(square_estimates__match_method=SquareEstimate.Match.CREATED)
    ).distinct()
    kept = (
        Job.objects.filter(Q(square_invoices__isnull=False) | Q(square_estimates__isnull=False))
        .exclude(pk__in=jobs.values("pk"))
        .distinct()
    )
    return {
        "customers": customers,
        "jobs": jobs,
        "unlinked_jobs": kept,
        "invoices": SquareInvoice.objects.all(),
        "orders": SquareOrder.objects.all(),
        "estimates": SquareEstimate.objects.all(),
        "estimate_emails": EstimateEmail.objects.all(),
    }


def counts(p: dict | None = None) -> dict:
    return {name: qs.count() for name, qs in (p or plan()).items()}


@transaction.atomic
def clear() -> dict:
    """Remove it all (see the module notes). Returns the counts removed."""
    from apps.jobs.models import Job

    from . import square

    p = plan()
    done = counts(p)
    customer_ids = list(p["customers"].values_list("pk", flat=True))
    job_ids = list(p["jobs"].values_list("pk", flat=True))
    kept_ids = list(p["unlinked_jobs"].values_list("pk", flat=True))
    estimate_numbers = list(SquareEstimate.objects.values_list("number", flat=True))

    AuditLog.objects.filter(
        Q(entity_type__in=_SQUARE_TYPES)
        | Q(job_id__in=job_ids)
        | Q(entity_type="jobs.job", entity_id__in=job_ids)
        | Q(entity_type="customers.customer", entity_id__in=customer_ids)
    ).delete()
    EstimateEmail.objects.all().delete()
    SquareEstimate.objects.all().delete()
    SquareOrder.objects.all().delete()
    SquareInvoice.objects.all().delete()
    Job.objects.filter(pk__in=job_ids).delete()
    p["customers"].model.objects.filter(pk__in=customer_ids).delete()

    for job in Job.objects.filter(pk__in=kept_ids):
        job.square_estimate_ref = ""
        if job.square_number in estimate_numbers:
            job.square_number = ""
        job.save(update_fields=["square_estimate_ref", "square_number", "updated_at"])
        square.refresh_job(job, event_id="clear_square_data")
    return done
