"""Editing a job's details, items, sizes, tasks and notes. Every change is audited and tied to
the job, so it shows on the job's timeline (FR-32, NFR-03).

Items, sizes and tasks stay editable until the job is In production (Step 1 spec). Status is
never changed here; see `services`.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils.text import slugify

from apps.audit.services import diff, record
from apps.production.models import DecorationMethod, ProductionTask

from .flow import PATH
from .models import ONE_SIZE, Job, JobItem, JobItemSize, JobNote, Status

DETAIL_FIELDS = [
    "title",
    "square_number",
    "square_estimate_ref",
    "contact",
    "due_date",
    "priority",
    "fulfillment_method",
    "shared_drive_path",
    "quote_sent_at",
    "quote_approved_at",
    "notifications_opt_out",
]
ITEM_FIELDS = ["item_no", "description", "color", "is_sized", "customer_supplied", "notes"]
TASK_FIELDS = ["job_item", "decoration_method", "placement", "notes", "outsourced", "sequence"]


class Locked(Exception):
    """The job is too far along for its items or tasks to change."""


def _require(user, perm: str) -> None:
    if not user.has_perm(perm):
        raise PermissionDenied(f"You don't have permission to do that ({perm}).")


def items_editable(job: Job) -> bool:
    """Until In production. A job on hold or cancelled counts from the stage it left."""
    status = job.resume_status if job.status in (Status.ON_HOLD, Status.CANCELLED) else job.status
    if job.status == Status.CANCELLED or status not in PATH:
        return False
    return PATH.index(status) < PATH.index(Status.IN_PRODUCTION)


def _require_editable(job: Job) -> None:
    if not items_editable(job):
        raise Locked("Items and tasks can't change once the job is in production.")


def _values(obj, fields) -> dict:
    out = {}
    for name in fields:
        value = getattr(obj, name)
        out[name] = str(value) if hasattr(value, "_meta") else value
    return out


def _size_map(item: JobItem) -> dict[str, int]:
    return {s.size: s.quantity for s in item.sizes.all()}


# --- Job details ------------------------------------------------------------------


@transaction.atomic
def update_details(job: Job, *, user, data: dict) -> Job:
    """`data` is cleaned form data for DETAIL_FIELDS."""
    _require(user, "jobs.change_job")
    job = Job.objects.select_for_update().get(pk=job.pk)
    square_number = (data.get("square_number") or "").strip()
    if (
        square_number
        and Job.objects.filter(square_number=square_number).exclude(pk=job.pk).exists()
    ):
        raise ValidationError({"square_number": f"Job {square_number} already exists."})
    before = _values(job, DETAIL_FIELDS)
    for name in DETAIL_FIELDS:
        if name in data:
            setattr(job, name, data[name])
    job.save()
    changes = diff(before, _values(job, DETAIL_FIELDS))
    if changes:
        record(user, job, "update", changes)
    return job


# --- Items and sizes --------------------------------------------------------------


def _write_sizes(item: JobItem, sizes: dict[str, int]) -> None:
    item.sizes.all().delete()
    JobItemSize.objects.bulk_create(
        JobItemSize(job_item=item, size=size, quantity=qty) for size, qty in sizes.items() if qty
    )


@transaction.atomic
def save_item(job: Job, *, user, data: dict, sizes: dict[str, int], item: JobItem | None = None):
    """Create or change a line. A line without sizes stores its quantity as one "OS" row."""
    _require(user, "jobs.change_job")
    _require_editable(job)
    if not data.get("is_sized", True):
        total = sum(sizes.values())
        sizes = {ONE_SIZE: total}
    sizes = {size.strip().upper(): qty for size, qty in sizes.items() if qty}
    if not sizes:
        raise ValidationError("Enter a quantity.")
    creating = item is None
    item = item or JobItem(job=job)
    before = {} if creating else {**_values(item, ITEM_FIELDS), "sizes": _size_map(item)}
    for name in ITEM_FIELDS:
        if name in data:
            setattr(item, name, data[name])
    item.save()
    _write_sizes(item, sizes)
    after = {**_values(item, ITEM_FIELDS), "sizes": sizes}
    if creating:
        record(
            user,
            item,
            "create",
            {"description": [None, item.description], "line": [None, item.line_no]},
            job=job,
        )
    elif changes := diff(before, after):
        changes["line"] = [item.line_no, item.line_no]
        record(user, item, "update", changes, job=job)
    # Tasks on this line plan for its full quantity until Step 3 tracks production.
    item.tasks.update(qty_planned=sum(sizes.values()))
    return item


@transaction.atomic
def delete_item(item: JobItem, *, user) -> None:
    _require(user, "jobs.change_job")
    _require_editable(item.job)
    record(
        user,
        item,
        "delete",
        {"description": [item.description, None], "line": [item.line_no, None]},
        job=item.job,
    )
    item.delete()


# --- Tasks ------------------------------------------------------------------------


@transaction.atomic
def add_method(*, user, name: str) -> DecorationMethod:
    """ "Add to list" from the task form (Admin only)."""
    _require(user, "production.add_decorationmethod")
    name = name.strip()
    existing = DecorationMethod.objects.filter(name__iexact=name).first()
    if existing:
        return existing
    code = slugify(name)[:30] or "method"
    base, n = code, 2
    while DecorationMethod.objects.filter(code=code).exists():
        code = f"{base[:27]}-{n}"
        n += 1
    method = DecorationMethod.objects.create(code=code, name=name)
    record(user, method, "create", {"name": [None, name]})
    return method


@transaction.atomic
def save_task(job: Job, *, user, data: dict, task: ProductionTask | None = None):
    """Create or change a decoration task: one item × one method (FR-21)."""
    _require(user, "jobs.change_job")
    _require_editable(job)
    item = data.get("job_item")
    if item is not None and item.job_id != job.pk:
        raise ValidationError("That item belongs to another job.")
    creating = task is None
    task = task or ProductionTask(job=job)
    before = {} if creating else _values(task, TASK_FIELDS)
    for name in TASK_FIELDS:
        if name in data and not (name == "sequence" and data[name] is None):
            setattr(task, name, data[name])
    if creating and not data.get("sequence"):
        last = ProductionTask.objects.filter(job=job).aggregate(n=Max("sequence"))["n"]
        task.sequence = (last or 0) + 1
    if task.decoration_method_id and (creating or "decoration_method" in data):
        task.station = task.decoration_method.stations.filter(is_active=True).first()
    task.qty_planned = item.total_quantity if item is not None else 0
    task.save()
    if creating:
        record(user, task, "create", {"task": [None, str(task)]}, job=job)
    elif changes := diff(before, _values(task, TASK_FIELDS)):
        changes["task"] = [str(task), str(task)]
        record(user, task, "update", changes, job=job)
    return task


@transaction.atomic
def delete_task(task: ProductionTask, *, user) -> None:
    _require(user, "jobs.change_job")
    _require_editable(task.job)
    record(user, task, "delete", {"task": [str(task), None]}, job=task.job)
    task.delete()


# --- Notes ------------------------------------------------------------------------


def add_note(job: Job, *, user, body: str) -> JobNote:
    _require(user, "jobs.add_jobnote")
    body = body.strip()
    if not body:
        raise ValidationError("The note is empty.")
    return JobNote.objects.create(job=job, author=user, body=body)
