from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.jobs.models import Job, JobItem

from . import services
from .models import PurchaseOrder, PurchaseOrderLine


def _say(request, exc) -> None:
    messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))


def _date(text: str):
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


@login_required
@permission_required(services.VIEW, raise_exception=True)
def po_list(request):
    show_all = request.GET.get("show") == "all"
    pos = PurchaseOrder.objects.prefetch_related("lines__job")
    if show_all:
        pos = pos.order_by("-created_at")[:200]
    else:
        pos = pos.filter(
            status__in=[
                PurchaseOrder.Status.DRAFT,
                PurchaseOrder.Status.ORDERED,
                PurchaseOrder.Status.PARTLY_RECEIVED,
            ]
        ).order_by("expected_at", "created_at")
    rows = []
    for po in pos:
        jobs = list(dict.fromkeys(line.job for line in po.lines.all() if line.job))
        rows.append({"po": po, "jobs": jobs})
    return render(request, "purchasing/po_list.html", {"rows": rows, "show_all": show_all})


@login_required
@permission_required(services.BUILD, raise_exception=True)
def po_build(request):
    if request.method == "POST":
        ids = [i for i in request.POST.getlist("jobs") if i.isdigit()]
        try:
            po = services.build(
                user=request.user,
                supplier_name=request.POST.get("supplier_name", ""),
                jobs=Job.objects.filter(pk__in=ids).order_by("pk"),
                kind=request.POST.get("kind") or PurchaseOrder.Kind.GARMENT,
                notes=request.POST.get("notes", ""),
            )
        except (ValidationError, PermissionDenied) as exc:
            _say(request, exc)
        else:
            messages.success(
                request, f"{po.po_number} drafted. Check the lines, then mark it ordered."
            )
            return redirect("purchasing:detail", pk=po.pk)
    candidates = [
        {"job": job, "needed": services.still_needed(job)} for job in services.candidates()
    ]
    chosen = set(request.POST.getlist("jobs") or request.GET.getlist("job"))
    return render(
        request,
        "purchasing/po_build.html",
        {"candidates": candidates, "chosen": chosen, "kinds": PurchaseOrder.Kind.choices},
    )


@login_required
@permission_required(services.VIEW, raise_exception=True)
def po_detail(request, pk, template="purchasing/po_detail.html"):
    po = get_object_or_404(PurchaseOrder, pk=pk)
    lines = list(
        po.lines.select_related("job", "job_item", "production_task").prefetch_related("receipts")
    )
    groups = {}
    for line in lines:
        groups.setdefault(line.job, []).append(line)
    jobs = Job.objects.filter(status__in=services.ORDERABLE).order_by("pk")
    return render(
        request,
        template,
        {
            "po": po,
            "groups": list(groups.items()),
            "open_lines": [line for line in lines if not line.is_complete],
            "can_edit": request.user.has_perm(services.EDIT),
            "can_receive": request.user.has_perm(services.RECEIVE),
            "editable": po.status
            not in (PurchaseOrder.Status.RECEIVED, PurchaseOrder.Status.CANCELLED),
            "line_jobs": jobs,
        },
    )


@login_required
@permission_required(services.VIEW, raise_exception=True)
def po_print(request, pk):
    return po_detail(request, pk, template="purchasing/po_print.html")


@login_required
@require_POST
def po_order(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk)
    try:
        blocked = services.mark_ordered(
            po,
            user=request.user,
            supplier_order_ref=request.POST.get("supplier_order_ref", ""),
            expected_at=_date(request.POST.get("expected_at", "")),
        )
    except (ValidationError, PermissionDenied) as exc:
        _say(request, exc)
    else:
        messages.success(request, f"{po.po_number} ordered.")
        for job, reason in blocked:
            messages.warning(request, f"{job.display_number} stays where it is: {reason}.")
    return redirect("purchasing:detail", pk=po.pk)


@login_required
@require_POST
def po_receive(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk)
    quantities = {
        key.removeprefix("qty_"): value
        for key, value in request.POST.items()
        if key.startswith("qty_")
    }
    try:
        released = services.receive(
            po, user=request.user, quantities=quantities, note=request.POST.get("note", "")
        )
    except (ValidationError, PermissionDenied) as exc:
        _say(request, exc)
    else:
        messages.success(request, "Delivery received.")
        for job in released:
            messages.success(request, f"{job.display_number} is Ready for production.")
    return redirect("purchasing:detail", pk=po.pk)


@login_required
@require_POST
def po_add_line(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk)
    job_id = request.POST.get("job", "")
    try:
        services.add_line(
            po,
            user=request.user,
            description=request.POST.get("description", ""),
            qty=request.POST.get("qty") or 0,
            job=Job.objects.filter(pk=job_id).first() if job_id.isdigit() else None,
            color=request.POST.get("color", ""),
            size=request.POST.get("size", ""),
        )
    except (ValidationError, PermissionDenied, ValueError) as exc:
        _say(request, exc)
    return redirect("purchasing:detail", pk=po.pk)


@login_required
@require_POST
def po_cancel(request, pk):
    po = get_object_or_404(PurchaseOrder, pk=pk)
    try:
        services.cancel(po, user=request.user, reason=request.POST.get("reason", ""))
    except (ValidationError, PermissionDenied) as exc:
        _say(request, exc)
    else:
        messages.success(request, f"{po.po_number} cancelled.")
    return redirect("purchasing:detail", pk=po.pk)


@login_required
@require_POST
def line_action(request, pk):
    line = get_object_or_404(PurchaseOrderLine.objects.select_related("purchase_order"), pk=pk)
    action = request.POST.get("action")
    try:
        if action == "qty":
            services.change_qty(line, user=request.user, qty=int(request.POST.get("qty") or 0))
        elif action == "remove":
            services.remove_line(line, user=request.user)
        elif action == "short":
            if services.close_short(line, user=request.user, reason=request.POST.get("reason", "")):
                messages.success(request, f"{line.job.display_number} is Ready for production.")
        else:
            raise ValidationError("Unknown action.")
    except (ValidationError, PermissionDenied, ValueError) as exc:
        _say(request, exc)
    return redirect("purchasing:detail", pk=line.purchase_order_id)


@login_required
@require_POST
def item_materials(request, pk):
    """From stock, or customer goods checked in, for one item (anyone on the floor)."""
    item = get_object_or_404(JobItem.objects.select_related("job"), pk=pk)
    on = request.POST.get("on") == "1"
    try:
        if request.POST.get("what") == "goods":
            released = services.check_in_goods(item, user=request.user, on=on)
        else:
            released = services.set_from_stock(item, user=request.user, on=on)
    except (ValidationError, PermissionDenied) as exc:
        _say(request, exc)
    else:
        if released:
            messages.success(request, f"{item.job.display_number} is Ready for production.")
    return redirect(f"{reverse('jobs:detail', args=[item.job_id])}?tab=items")
