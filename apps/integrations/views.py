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

from . import linking, square, tasks
from .forms import PaymentGatesForm
from .models import SquareInvoice, WebhookEvent

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
    invoices = linking.waiting().order_by("-square_updated_at", "-id")
    return render(request, "integrations/inbox.html", {"invoices": invoices})


def _back(request, invoice):
    target = request.POST.get("next", "")
    if target.startswith("/") and not target.startswith("//"):
        return redirect(target)
    return redirect("integrations:inbox")


@login_required
@require_POST
def invoice_link(request, pk):
    invoice = get_object_or_404(SquareInvoice, pk=pk)
    number = request.POST.get("job_number", "")
    job = board.exact_number(number)
    if job is None:
        messages.error(request, f"No single job has the number “{number.strip()}”.")
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
            request, f"Invoice {invoice.invoice_number} unlinked; it's in Invoices to link."
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
