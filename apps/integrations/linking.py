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
    square.refresh_job(previous, invoice=invoice)
    return invoice


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
