import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.accounts.models import AppSetting
from apps.audit.services import record
from apps.jobs import board, gates

from . import estimates, linking, square, tasks
from .forms import PaymentGatesForm
from .models import SquareEstimate, SquareInvoice, WebhookEvent

log = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def square_webhook(request):
    """Square's deliveries (INT-01): checked, stored once, then handed to the worker.
    Answers at once so Square doesn't time out and retry."""
    if not settings.SQUARE_WEBHOOK_SIGNATURE_KEY:
        raise Http404
    url = settings.SQUARE_WEBHOOK_URL or request.build_absolute_uri()
    signature = request.headers.get("x-square-hmacsha256-signature", "")
    if not square.valid_signature(request.body, signature, url):
        log.warning("Square webhook refused: bad or missing signature")
        return HttpResponse(status=403)
    try:
        payload = json.loads(request.body)
        event_id = str(payload["event_id"])
        event_type = str(payload.get("type") or "")
    except (ValueError, KeyError, TypeError):
        return HttpResponse(status=400)
    data = payload.get("data") or {}
    with transaction.atomic():
        event, created = WebhookEvent.objects.get_or_create(
            event_id=event_id,
            defaults={
                "event_type": event_type[:60],
                "square_object_id": str(data.get("id") or "")[:100],
                "body": payload,
            },
        )
        if created:
            transaction.on_commit(lambda: tasks.process_webhook_event.defer(event_pk=event.pk))
    return HttpResponse(status=200)


# --- The "Link to job" inbox (FR-04) -----------------------------------------------


@login_required
@permission_required(linking.PERMISSION, raise_exception=True)
def inbox(request):
    invoices = list(linking.waiting().order_by("-square_updated_at", "-id"))
    waiting_estimates = list(estimates.waiting())
    open_jobs = linking.open_jobs()
    for invoice in invoices:
        invoice.suggested = linking.likely_jobs(
            open_jobs, name=invoice.recipient_name, square_customer_id=invoice.square_customer_id
        )
    for estimate in waiting_estimates:
        estimate.suggested = linking.likely_jobs(
            open_jobs, name=estimate.customer_name, email=estimate.customer_email
        )
    return render(
        request,
        "integrations/inbox.html",
        {"invoices": invoices, "estimates": waiting_estimates, "open_jobs": open_jobs},
    )


def _picked_job(request):
    """The job chosen from "Browse open jobs", else the one with the number typed in.
    Returns (job, error message)."""
    from apps.jobs.models import Job

    picked = request.POST.get("job_id", "")
    if picked:
        job = Job.objects.filter(pk=picked, pk__in=[j.pk for j in linking.open_jobs()]).first()
        return job, "" if job else "That job isn't open any more. Pick another."
    number = request.POST.get("job_number", "")
    job = board.exact_number(number)
    return job, "" if job else f"No single job has the number “{number.strip()}”."


def _back(request, invoice):
    target = request.POST.get("next", "")
    if target.startswith("/") and not target.startswith("//"):
        return redirect(target)
    return redirect("integrations:inbox")


@login_required
@require_POST
def invoice_link(request, pk):
    invoice = get_object_or_404(SquareInvoice, pk=pk)
    job, error = _picked_job(request)
    if job is None:
        messages.error(request, error)
        return _back(request, invoice)
    try:
        linking.link(invoice, job, user=request.user)
    except PermissionDenied as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request, f"Invoice {invoice.invoice_number} is now on {job.display_number}."
        )
    return _back(request, invoice)


@login_required
def invoice_new_job(request, pk):
    """Create a job from an invoice that matched none: confirm the title and customer."""
    invoice = get_object_or_404(SquareInvoice, pk=pk)
    if not (request.user.has_perm(linking.PERMISSION) and request.user.has_perm("jobs.add_job")):
        raise PermissionDenied
    linked = linking.customer_for(invoice)
    candidates = [] if linked else list(linking.same_name(invoice))
    if request.method == "POST":
        picked = None
        choice = request.POST.get("customer", "new")
        if not linked and choice != "new":
            picked = next((c for c in candidates if str(c.pk) == choice), None)
            if picked is None:
                messages.error(request, "Pick one of the customers listed, or a new one.")
                return redirect("integrations:invoice_new_job", pk=invoice.pk)
        try:
            job = linking.create_job_from(
                invoice, user=request.user, title=request.POST.get("title", ""), customer=picked
            )
        except PermissionDenied as exc:
            messages.error(request, str(exc))
            return redirect("integrations:inbox")
        messages.success(
            request,
            f"Job {job.display_number} created from invoice {invoice.invoice_number}.",
        )
        return redirect("jobs:detail", pk=job.pk)
    return render(
        request,
        "integrations/new_job.html",
        {"invoice": invoice, "linked": linked, "candidates": candidates},
    )


@login_required
@require_POST
def invoice_unlink(request, pk):
    invoice = get_object_or_404(SquareInvoice, pk=pk)
    job = invoice.job
    try:
        linking.unlink(invoice, user=request.user)
    except PermissionDenied as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request, f"Invoice {invoice.invoice_number} unlinked; it's back in Invoices."
        )
    if job is not None:
        return redirect(f"/jobs/{job.pk}/?tab=details")
    return redirect("integrations:inbox")


@login_required
@require_POST
def invoice_set_aside(request, pk):
    invoice = get_object_or_404(SquareInvoice, pk=pk)
    try:
        linking.set_aside(invoice, user=request.user)
    except PermissionDenied as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Invoice {invoice.invoice_number} set aside: not a job.")
    return redirect("integrations:inbox")


# --- Square estimates waiting to be linked ----------------------------------------


@login_required
@require_POST
def estimate_link(request, pk):
    estimate = get_object_or_404(SquareEstimate, pk=pk)
    job, error = _picked_job(request)
    if job is None:
        messages.error(request, error)
        return redirect("integrations:inbox")
    try:
        estimates.link(estimate, job, user=request.user)
    except PermissionDenied as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Estimate {estimate.number} is now on {job.display_number}.")
    return redirect("integrations:inbox")


@login_required
def estimate_new_job(request, pk):
    """Create a job for an estimate: confirm the title and the customer."""
    from apps.customers.models import Customer

    estimate = get_object_or_404(SquareEstimate, pk=pk)
    if not (request.user.has_perm(linking.PERMISSION) and request.user.has_perm("jobs.add_job")):
        raise PermissionDenied
    candidates = list(
        Customer.objects.filter(email__iexact=estimate.customer_email)
        if estimate.customer_email
        else Customer.objects.none()
    ) or list(Customer.objects.filter(display_name__iexact=estimate.customer_name or "-"))
    if request.method == "POST":
        choice = request.POST.get("customer", "new")
        customer = next((c for c in candidates if str(c.pk) == choice), None)
        if customer is None and choice != "new":
            messages.error(request, "Pick one of the customers listed, or a new one.")
            return redirect("integrations:estimate_new_job", pk=estimate.pk)
        if customer is None:
            if not request.user.has_perm("customers.add_customer"):
                raise PermissionDenied
            customer = Customer.objects.create(
                display_name=(estimate.customer_name or estimate.customer_email or "New")[:200],
                email=estimate.customer_email,
            )
            record(
                request.user,
                customer,
                "create",
                {"source": [None, f"Square estimate {estimate.number}"]},
            )
        try:
            job = estimates.create_job(
                estimate,
                user=request.user,
                customer=customer,
                title=request.POST.get("title", ""),
            )
        except PermissionDenied as exc:
            messages.error(request, str(exc))
            return redirect("integrations:inbox")
        messages.success(
            request, f"Job {job.display_number} created from estimate {estimate.number}."
        )
        return redirect("jobs:detail", pk=job.pk)
    return render(
        request,
        "integrations/estimate_new_job.html",
        {
            "estimate": estimate,
            "candidates": candidates,
            "title": estimates.title_for(
                estimate.customer_name or estimate.customer_email, estimate.number
            ),
        },
    )


@login_required
@require_POST
def estimate_set_aside(request, pk):
    estimate = get_object_or_404(SquareEstimate, pk=pk)
    try:
        estimates.set_aside(estimate, user=request.user)
    except PermissionDenied as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Estimate {estimate.number} set aside: not a job.")
    return redirect("integrations:inbox")


# --- Payment gates (FR-07) ---------------------------------------------------------


@login_required
@permission_required("accounts.change_appsetting", raise_exception=True)
def payment_gates(request):
    before = gates.configured()
    form = PaymentGatesForm(request.POST or None, current=before)
    if request.method == "POST" and form.is_valid():
        after = form.gates()
        setting, _ = AppSetting.objects.get_or_create(key=gates.SETTING, defaults={"value": before})
        setting.value = after
        setting.updated_by = request.user
        setting.save()
        if after != before:
            record(request.user, setting, "update", {"value": [before, after]})
        messages.success(request, "Payment gates saved.")
        return redirect("integrations:payment_gates")
    return render(
        request,
        "integrations/payment_gates.html",
        {"form": form, "square_on": settings.SQUARE_ENABLED},
    )
