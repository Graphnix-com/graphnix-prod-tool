"""Linking Square invoices to jobs by hand: the "Link to job" inbox and the job page (FR-04).

An invoice links itself when its title carries a job's G- number. These are for the rest:
link it to a job, unlink a wrong match, or set it aside as "not a job" (a sale with no
production). Each is audited under the person who did it. An invoice unlinked by hand never
links itself again; it waits in the inbox.
"""

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from apps.audit.services import record

from . import square
from .models import SquareInvoice

PERMISSION = "integrations.link_square_invoice"


def waiting():
    """Invoices in the inbox: no job, not set aside, not cancelled or deleted."""
    return SquareInvoice.objects.filter(job__isnull=True, not_a_job_at__isnull=True).exclude(
        status__in=square.CANCELLED_STATES
    )


def open_jobs() -> list:
    """Jobs an invoice or estimate can go on, for "Browse open jobs": not shipped, closed or
    cancelled. By customer, then newest first."""
    from apps.jobs.models import Job, Status

    return list(
        Job.objects.exclude(status__in=[Status.SHIPPED, Status.CLOSED, Status.CANCELLED])
        .select_related("customer", "contact")
        .order_by("customer__display_name", "-created_at")
    )


def likely_jobs(jobs, *, name: str = "", email: str = "", square_customer_id: str = "") -> list:
    """The open jobs that look like this customer's, listed first when browsing."""
    name = " ".join((name or "").split()).lower()
    email = (email or "").strip().lower()

    def matches(job) -> bool:
        customer = job.customer
        if square_customer_id and customer.square_customer_id == square_customer_id:
            return True
        names = {customer.display_name.lower(), customer.company_name.lower()}
        if job.contact:
            names.add(job.contact.full_name.lower())
        emails = {customer.email.lower(), (job.contact.email if job.contact else "").lower()}
        return bool((name and name in names) or (email and email in emails))

    return [j for j in jobs if matches(j)]


def _require(user):
    if not user.has_perm(PERMISSION):
        raise PermissionDenied("You can't link Square invoices.")


def _locked(invoice) -> SquareInvoice:
    return SquareInvoice.objects.select_for_update().get(pk=invoice.pk)


@transaction.atomic
def link(invoice: SquareInvoice, job, *, user) -> SquareInvoice:
    _require(user)
    invoice = _locked(invoice)
    previous = invoice.job
    if previous == job:
        return invoice
    invoice.job = job
    invoice.match_method = SquareInvoice.Match.MANUAL
    invoice.linked_by = user
    invoice.linked_at = timezone.now()
    invoice.not_a_job_by = None
    invoice.not_a_job_at = None
    invoice.save()
    record(user, invoice, "link", {"invoice": [None, invoice.invoice_number]}, job=job)
    if previous is not None:
        record(user, invoice, "unlink", {"invoice": [invoice.invoice_number, None]}, job=previous)
        square.refresh_job(previous, invoice=invoice)
    square.match_customer(job.customer, invoice.square_customer_id, how="invoice")
    square.refresh_job(job, invoice=invoice)
    return invoice


@transaction.atomic
def unlink(invoice: SquareInvoice, *, user) -> SquareInvoice:
    _require(user)
    invoice = _locked(invoice)
    previous = invoice.job
    if previous is None:
        return invoice
    invoice.job = None
    invoice.match_method = SquareInvoice.Match.UNLINKED
    invoice.linked_by = None
    invoice.linked_at = None
    invoice.save()
    record(user, invoice, "unlink", {"invoice": [invoice.invoice_number, None]}, job=previous)
    _give_back_number(previous, invoice, user=user)
    square.refresh_job(previous, invoice=invoice)
    return invoice


def _give_back_number(job, invoice: SquareInvoice, *, user) -> None:
    """A job numbered after an invoice that is unlinked goes back to its estimate number,
    else its G- number."""
    from apps.jobs.models import Job

    if not invoice.invoice_number or job.square_number != invoice.invoice_number:
        return
    number = job.square_estimate_ref
    if number and Job.objects.filter(square_number=number).exclude(pk=job.pk).exists():
        number = ""
    Job.objects.filter(pk=job.pk).update(square_number=number)
    record(user, job, "update", {"square_number": [invoice.invoice_number, number or None]})


@transaction.atomic
def set_aside(invoice: SquareInvoice, *, user) -> SquareInvoice:
    """Not a job: kept, out of the inbox."""
    _require(user)
    invoice = _locked(invoice)
    if invoice.job_id is not None:
        raise PermissionDenied("Unlink the invoice from its job first.")
    invoice.not_a_job_by = user
    invoice.not_a_job_at = timezone.now()
    invoice.save(update_fields=["not_a_job_by", "not_a_job_at", "updated_at"])
    record(user, invoice, "not_a_job", {"invoice": [None, invoice.invoice_number]})
    return invoice


# --- A new job from an invoice -----------------------------------------------------


def customer_for(invoice: SquareInvoice):
    """The tracker customer linked to the invoice's Square customer, if any."""
    from apps.customers.models import Customer

    if not invoice.square_customer_id:
        return None
    return Customer.objects.filter(square_customer_id=invoice.square_customer_id).first()


def same_name(invoice: SquareInvoice):
    """Tracker customers named like the invoice's recipient, to pick from."""
    from apps.customers.models import Customer

    name = " ".join((invoice.recipient_name or "").split())
    if not name:
        return Customer.objects.none()
    return Customer.objects.filter(display_name__iexact=name).order_by("email", "pk")


@transaction.atomic
def create_job_from(invoice: SquareInvoice, *, user, title: str, customer=None):
    """A new job at Invoiced for an invoice that matched none, linked to it. The customer is
    the one linked in Square, else the one picked, else a new one named as on the invoice."""
    from apps.customers.models import Customer
    from apps.jobs.models import Status
    from apps.jobs.services import create_job

    _require(user)
    invoice = _locked(invoice)
    if invoice.job_id is not None:
        raise PermissionDenied("This invoice is already on a job.")
    if invoice.status in square.CANCELLED_STATES:
        raise PermissionDenied("This invoice is cancelled in Square.")
    customer = customer_for(invoice) or customer
    if customer is None:
        if not user.has_perm("customers.add_customer"):
            raise PermissionDenied("You can't add customers.")
        customer = Customer.objects.create(
            display_name=(invoice.recipient_name or f"Invoice {invoice.invoice_number}")[:200]
        )
        record(
            user,
            customer,
            "create",
            {"display_name": [None, customer.display_name], "source": [None, "Square invoice"]},
        )
    title = (title or "").strip() or invoice.title or f"Invoice {invoice.invoice_number}"
    job = create_job(customer=customer, title=title[:200], start=Status.INVOICED, user=user)
    link(invoice, job, user=user)
    return job
