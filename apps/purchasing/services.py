"""Building, ordering and receiving purchase orders, and releasing jobs to production
(FR-13 to FR-16; Step 3 spec, "Purchasing and receiving").

- Only jobs past Mock-up approved can go on a PO (the mock-up gate, FR-12).
- Marking a PO ordered moves its jobs from Mock-up approved to Materials ordered.
- A job moves to Ready for production by itself when every item is accounted for: received
  on a PO (or its line closed short), ticked From stock, or customer-supplied and checked
  in; and any other line for the job (consumables, outsourced work) is complete.
Both moves go through the payment gates; a job a gate stops stays where it is.
"""

from __future__ import annotations

from collections import defaultdict

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.audit.services import record
from apps.jobs import services as job_services
from apps.jobs.models import Job, JobItem, Status

from .models import PurchaseOrder, PurchaseOrderLine, Receipt

BUILD = "purchasing.add_purchaseorder"
EDIT = "purchasing.change_purchaseorder"
VIEW = "purchasing.view_purchaseorder"
RECEIVE = "purchasing.receive_purchaseorder"
CHECK_IN = "jobs.check_in_materials"

# Jobs whose items may go on a PO: past the mock-up gate, not yet released.
ORDERABLE = (Status.MOCKUP_APPROVED, Status.MATERIALS_ORDERED)
# POs whose lines count: placed with the supplier (a draft isn't ordered yet).
PLACED = (
    PurchaseOrder.Status.ORDERED,
    PurchaseOrder.Status.PARTLY_RECEIVED,
    PurchaseOrder.Status.RECEIVED,
)


def _require(user, perm: str, message: str) -> None:
    if not user.has_perm(perm):
        raise PermissionDenied(message)


def _active_lines():
    return PurchaseOrderLine.objects.exclude(purchase_order__status=PurchaseOrder.Status.CANCELLED)


# --- What still needs ordering ------------------------------------------------------


def still_needed(job: Job) -> list[dict]:
    """Per item and size, what isn't on a PO yet: customer-supplied and From stock items
    are never needed."""
    on_order = defaultdict(int)
    for row in (
        _active_lines()
        .filter(job=job, job_item__isnull=False)
        .values("job_item_id", "size")
        .annotate(n=Sum("qty_ordered"))
    ):
        on_order[(row["job_item_id"], row["size"])] = row["n"]
    needed = []
    for item in job.items.prefetch_related("sizes"):
        if item.customer_supplied or item.from_stock_at:
            continue
        for s in item.sizes.all():
            qty = s.quantity - on_order[(item.pk, s.size)]
            if qty > 0:
                needed.append({"item": item, "size": s.size, "qty": qty})
    return needed


def candidates():
    """Jobs that could go on a new PO, soonest due first."""
    jobs = (
        Job.objects.filter(status__in=ORDERABLE)
        .select_related("customer")
        .order_by("due_date", "status_changed_at")
    )
    return [j for j in jobs if still_needed(j) or _outsourced_unordered(j)]


def _outsourced_unordered(job: Job):
    return list(job.tasks.filter(outsourced=True).exclude(po_lines__in=_active_lines()))


# --- Build ---------------------------------------------------------------------------


@transaction.atomic
def build(*, user, supplier_name: str, jobs, kind=PurchaseOrder.Kind.GARMENT, notes=""):
    """A draft PO for these jobs, its lines filled from what each still needs."""
    _require(user, BUILD, "Only Admin and Manager can build purchase orders.")
    supplier_name = " ".join((supplier_name or "").split())[:200]
    if not supplier_name:
        raise ValidationError("Say who the order is with.")
    jobs = list(jobs)
    if not jobs:
        raise ValidationError("Pick at least one job.")
    for job in jobs:
        if job.status not in ORDERABLE:
            raise ValidationError(
                f"{job.display_number} is at {job.get_status_display()}: only jobs past "
                "Mock-up approved, and not yet released, can go on a PO."
            )
    po = PurchaseOrder.objects.create(
        supplier_name=supplier_name, kind=kind, notes=(notes or "").strip(), created_by=user
    )
    for job in jobs:
        for need in still_needed(job):
            item = need["item"]
            PurchaseOrderLine.objects.create(
                purchase_order=po,
                job=job,
                job_item=item,
                description=item.description,
                item_no=item.item_no,
                color=item.color,
                size="" if need["size"] == "OS" else need["size"],
                qty_ordered=need["qty"],
            )
        for task in _outsourced_unordered(job):
            PurchaseOrderLine.objects.create(
                purchase_order=po,
                job=job,
                production_task=task,
                description=f"{task} (outsourced)"[:200],
                qty_ordered=task.qty_planned or 1,
            )
    if not po.lines.exists():
        raise ValidationError("Nothing is left to order for those jobs.")
    for job in jobs:
        record(user, po, "create", {"po": [None, po.po_number]}, job=job)
    return po


def _editable(po: PurchaseOrder) -> None:
    if po.status in (PurchaseOrder.Status.RECEIVED, PurchaseOrder.Status.CANCELLED):
        raise ValidationError(f"{po.po_number} is {po.get_status_display().lower()}.")


@transaction.atomic
def add_line(po, *, user, description, qty, job=None, color="", size="", item_no=""):
    """A consumable (for a job, or for stock without one) or anything else to order."""
    _require(user, EDIT, "Only Admin and Manager can change purchase orders.")
    _editable(po)
    description = (description or "").strip()[:200]
    if not description:
        raise ValidationError("Describe what's ordered.")
    if not qty or int(qty) < 1:
        raise ValidationError("Order at least one.")
    line = PurchaseOrderLine.objects.create(
        purchase_order=po,
        job=job,
        description=description,
        item_no=(item_no or "")[:60],
        color=(color or "")[:60],
        size=(size or "")[:10],
        qty_ordered=int(qty),
    )
    record(
        user, po, "update", {"po": [po.po_number, po.po_number], "line": [None, str(line)]}, job=job
    )
    if po.is_open:
        _update_status(po)
    return line


@transaction.atomic
def change_qty(line, *, user, qty: int) -> None:
    _require(user, EDIT, "Only Admin and Manager can change purchase orders.")
    _editable(line.purchase_order)
    if int(qty) < max(line.qty_received, 1):
        raise ValidationError(f"{line}: can't order fewer than {max(line.qty_received, 1)}.")
    old, line.qty_ordered = line.qty_ordered, int(qty)
    line.save(update_fields=["qty_ordered"])
    record(
        user,
        line.purchase_order,
        "update",
        {"po": [line.purchase_order.po_number] * 2, str(line): [old, line.qty_ordered]},
        job=line.job,
    )


@transaction.atomic
def remove_line(line, *, user) -> None:
    _require(user, EDIT, "Only Admin and Manager can change purchase orders.")
    _editable(line.purchase_order)
    if line.receipts.exists():
        raise ValidationError(f"{line}: some has arrived already; close it short instead.")
    po, job, name = line.purchase_order, line.job, str(line)
    line.delete()
    record(user, po, "update", {"po": [po.po_number] * 2, "line": [name, None]}, job=job)
    if po.is_open:
        _update_status(po)
        if job is not None:
            release_if_ready(job, user=user)


# --- Ordered ---------------------------------------------------------------------------


@transaction.atomic
def mark_ordered(po, *, user, supplier_order_ref="", expected_at=None) -> list[tuple]:
    """The order is placed: each job on it at Mock-up approved moves to Materials ordered.
    Returns (job, reason) for any job a payment gate kept where it was."""
    _require(user, EDIT, "Only Admin and Manager can mark purchase orders ordered.")
    po = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if po.status != PurchaseOrder.Status.DRAFT:
        raise ValidationError(f"{po.po_number} is already {po.get_status_display().lower()}.")
    if not po.lines.exists():
        raise ValidationError("Add something to order first.")
    po.status = PurchaseOrder.Status.ORDERED
    po.ordered_at = timezone.now()
    po.supplier_order_ref = (supplier_order_ref or "").strip()[:100]
    po.expected_at = expected_at
    po.save()
    blocked = []
    for job in _jobs_on(po):
        record(user, po, "ordered", {"po": [None, po.po_number]}, job=job)
        if job.status == Status.MOCKUP_APPROVED:
            reason = job_services.system_move(
                job, Status.MATERIALS_ORDERED, note=f"{po.po_number} ordered", user=user
            )
            if reason:
                blocked.append((job, reason))
    return blocked


def _jobs_on(po) -> list[Job]:
    ids = po.lines.filter(job__isnull=False).values_list("job_id", flat=True).distinct()
    return list(Job.objects.filter(pk__in=ids).order_by("pk"))


@transaction.atomic
def cancel(po, *, user, reason: str) -> None:
    _require(user, EDIT, "Only Admin and Manager can cancel purchase orders.")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Give a reason.")
    if Receipt.objects.filter(line__purchase_order=po).exists():
        raise ValidationError("Some of it has arrived: close the open lines short instead.")
    _editable(po)
    po.status = PurchaseOrder.Status.CANCELLED
    po.save(update_fields=["status"])
    for job in _jobs_on(po):
        record(user, po, "cancel", {"po": [None, po.po_number], "reason": [None, reason]}, job=job)


# --- Receiving -------------------------------------------------------------------------


def _update_status(po) -> None:
    lines = list(po.lines.all())
    done = lines and all(line.is_complete for line in lines)
    some = any(line.qty_received for line in lines)
    po.status = (
        PurchaseOrder.Status.RECEIVED
        if done
        else PurchaseOrder.Status.PARTLY_RECEIVED
        if some
        else PurchaseOrder.Status.ORDERED
    )
    po.save(update_fields=["status"])


@transaction.atomic
def receive(po, *, user, quantities: dict, note: str = "") -> list[Job]:
    """What arrived, by line id. Returns the jobs this released to Ready for production."""
    _require(user, RECEIVE, "You can't receive deliveries.")
    po = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if not po.is_open:
        raise ValidationError(f"{po.po_number} is {po.get_status_display().lower()}.")
    lines = {line.pk: line for line in po.lines.select_related("production_task")}
    got = {}
    for key, value in quantities.items():
        try:
            line_id, qty = int(key), int(value or 0)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Quantities must be whole numbers.") from exc
        if qty < 0:
            raise ValidationError("Quantities can't be negative.")
        if qty and line_id in lines:
            got[line_id] = qty
    if not got:
        raise ValidationError("Enter how many arrived.")
    now = timezone.now()
    touched = set()
    for line_id, qty in got.items():
        line = lines[line_id]
        Receipt.objects.create(
            line=line, qty=qty, received_at=now, received_by=user, note=note.strip()[:300]
        )
        line.qty_received += qty
        line.save(update_fields=["qty_received"])
        if line.production_task_id and line.is_complete:
            _outsourced_done(line.production_task, now)
        if line.job_id:
            touched.add(line.job_id)
            record(
                user,
                po,
                "received",
                {"po": [None, po.po_number], "line": [None, f"{line}: {qty}"]},
                job=line.job,
            )
    _update_status(po)
    return [j for j in Job.objects.filter(pk__in=touched) if release_if_ready(j, user=user)]


def _outsourced_done(task, now) -> None:
    from apps.production.models import TaskStatus

    task.status = TaskStatus.DONE
    task.completed_at = task.completed_at or now
    task.save(update_fields=["status", "completed_at", "updated_at"])


@transaction.atomic
def close_short(line, *, user, reason: str) -> bool:
    """This line will never fill (a size backordered and swapped): it counts as complete."""
    _require(user, EDIT, "Only Admin and Manager can close a line short.")
    reason = (reason or "").strip()[:300]
    if not reason:
        raise ValidationError("Say why it won't arrive.")
    po = PurchaseOrder.objects.select_for_update().get(pk=line.purchase_order_id)
    if not po.is_open:
        raise ValidationError(f"{po.po_number} is {po.get_status_display().lower()}.")
    line.closed_short_reason = reason
    line.save(update_fields=["closed_short_reason"])
    record(
        user,
        po,
        "closed_short",
        {"po": [None, po.po_number], "line": [None, f"{line}: {reason}"]},
        job=line.job,
    )
    _update_status(po)
    return bool(line.job_id) and release_if_ready(line.job, user=user)


# --- From stock and customer goods -----------------------------------------------------


@transaction.atomic
def set_from_stock(item: JobItem, *, user, on: bool = True) -> bool:
    _require(user, CHECK_IN, "You can't change where an item's blanks come from.")
    if item.customer_supplied:
        raise ValidationError("A customer-supplied item is checked in, not taken from stock.")
    if on and _active_lines().filter(job_item=item).exists():
        raise ValidationError(f"{item.description} is on a purchase order already.")
    if item.job.status not in (*ORDERABLE, Status.INVOICED, Status.MOCKUP_SENT):
        raise ValidationError("Only before the job is released to production.")
    old = bool(item.from_stock_at)
    item.from_stock_at = timezone.now() if on else None
    item.from_stock_by = user if on else None
    item.save(update_fields=["from_stock_at", "from_stock_by"])
    record(
        user,
        item,
        "update",
        {
            "line": [item.line_no, item.line_no],
            "from_stock": ["yes" if old else "no", "yes" if on else "no"],
        },
        job=item.job,
    )
    return on and release_if_ready(item.job, user=user)


@transaction.atomic
def check_in_goods(item: JobItem, *, user, on: bool = True) -> bool:
    _require(user, CHECK_IN, "You can't check in customer goods.")
    if not item.customer_supplied:
        raise ValidationError("Only customer-supplied items are checked in.")
    old = bool(item.customer_goods_received_at)
    item.customer_goods_received_at = timezone.now() if on else None
    item.save(update_fields=["customer_goods_received_at"])
    record(
        user,
        item,
        "update",
        {
            "line": [item.line_no, item.line_no],
            "customer_goods": ["in" if old else "not in", "in" if on else "not in"],
        },
        job=item.job,
    )
    return on and release_if_ready(item.job, user=user)


# --- Released ----------------------------------------------------------------------------


def item_ready(item: JobItem, lines) -> bool:
    if item.customer_supplied:
        return bool(item.customer_goods_received_at)
    if item.from_stock_at:
        return True
    mine = [line for line in lines if line.job_item_id == item.pk]
    return bool(mine) and all(
        line.purchase_order.status in PLACED and line.is_complete for line in mine
    )


def readiness(job: Job) -> dict:
    """Each item's state, and whether the whole job is ready for production."""
    lines = list(_active_lines().filter(job=job).select_related("purchase_order"))
    items = list(job.items.all())
    other = [line for line in lines if line.job_item_id is None]
    ready = (
        bool(items)
        and all(item_ready(item, lines) for item in items)
        and all(line.purchase_order.status in PLACED and line.is_complete for line in other)
    )
    return {"lines": lines, "items": items, "ready": ready}


def release_if_ready(job: Job, *, user=None) -> bool:
    """Move the job to Ready for production when everything is in. True if it moved."""
    job.refresh_from_db()
    if job.status not in ORDERABLE or not readiness(job)["ready"]:
        return False
    reason = job_services.system_move(
        job, Status.READY_FOR_PRODUCTION, note="All materials in", user=user
    )
    return not reason


# --- For the job page ----------------------------------------------------------------------


def for_job(job: Job) -> dict:
    """Each item's materials in a few words, and the job's POs, for the job page."""
    lines = list(_active_lines().filter(job=job).select_related("purchase_order"))
    items = {}
    for item in job.items.all():
        mine = [line for line in lines if line.job_item_id == item.pk]
        if item.customer_supplied:
            state = "in" if item.customer_goods_received_at else "waiting"
            text = "Customer goods checked in" if state == "in" else "Customer goods not in yet"
        elif item.from_stock_at:
            state, text = "in", "From stock"
        elif mine:
            pos = {}
            for line in mine:
                po = line.purchase_order
                got, want = pos.get(po, (0, 0))
                pos[po] = (got + min(line.qty_received, line.qty_ordered), want + line.qty_ordered)
            parts = [
                f"{po.po_number} draft"
                if po.status == PurchaseOrder.Status.DRAFT
                else f"{po.po_number}: {got} of {want} in"
                for po, (got, want) in pos.items()
            ]
            state = "in" if item_ready(item, lines) else "ordered"
            text = "; ".join(parts)
        else:
            state, text = "none", "Not ordered"
        items[item.pk] = {"state": state, "text": text}
    pos = list(dict.fromkeys(line.purchase_order for line in lines))
    return {"by_item": items, "pos": pos}
