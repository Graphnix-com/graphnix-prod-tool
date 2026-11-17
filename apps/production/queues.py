"""Station queues and My tasks (FR-22, FR-23; Step 3 spec, "Production floor").

A queue holds the tasks of released jobs (Ready for production, In production) that aren't
done, at that station. Order: rework first, then Sooner-if-possible jobs, then a Manager's
own order (when they've moved tasks), then due date. Start, pause and done are not built
(decided 17 Nov 2026): jobs move through production with the job page's buttons.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.audit.services import record
from apps.jobs.models import Status

from .models import ProductionTask, Station, TaskStatus

RELEASED = (Status.READY_FOR_PRODUCTION, Status.IN_PRODUCTION)
PLAN = "production.plan_queue"
_LAST = Decimal("1e9")


def _base():
    return (
        ProductionTask.objects.filter(job__status__in=RELEASED, outsourced=False)
        .exclude(status=TaskStatus.DONE)
        .select_related(
            "job", "job__customer", "job_item", "decoration_method", "assigned_to", "depends_on"
        )
        .prefetch_related("job_item__sizes")
    )


def _key(task: ProductionTask):
    job = task.job
    return (
        task.status != TaskStatus.REWORK,
        job.priority,
        task.queue_rank if task.queue_rank is not None else _LAST,
        job.due_date or date.max,
        job.status_changed_at,
        task.sequence,
        task.pk,
    )


def queue(station: Station) -> list[ProductionTask]:
    return sorted(_base().filter(station=station), key=_key)


def stations() -> list[dict]:
    """Every active station with how many tasks wait there."""
    counts = {}
    for task in _base():
        counts[task.station_id] = counts.get(task.station_id, 0) + 1
    return [
        {"station": s, "count": counts.get(s.pk, 0)}
        for s in Station.objects.filter(is_active=True).select_related("decoration_method")
    ]


def mine(user) -> dict:
    """The person's own tasks, then the unassigned ones at their usual station."""
    own = sorted(_base().filter(assigned_to=user), key=_key)
    station = getattr(user, "default_station", None)
    open_ = (
        sorted(_base().filter(assigned_to__isnull=True, station=station), key=_key)
        if station
        else []
    )
    return {"own": own, "unassigned": open_, "station": station}


@transaction.atomic
def move(task: ProductionTask, *, user, up: bool) -> None:
    """Swap the task with its neighbour among tasks of the same priority, and fix the whole
    group's order (ranks 1, 2, 3 ...) so it stays as the Manager left it."""
    if not user.has_perm(PLAN):
        raise PermissionDenied("Only Admin and Manager can reorder a queue.")
    tasks = [t for t in queue(task.station) if t.job.priority == task.job.priority]
    if task.status == TaskStatus.REWORK:
        tasks = [t for t in tasks if t.status == TaskStatus.REWORK]
    else:
        tasks = [t for t in tasks if t.status != TaskStatus.REWORK]
    i = next((n for n, t in enumerate(tasks) if t.pk == task.pk), None)
    j = None if i is None else (i - 1 if up else i + 1)
    if i is None or j is None or not 0 <= j < len(tasks):
        return
    tasks[i], tasks[j] = tasks[j], tasks[i]
    for rank, t in enumerate(tasks, 1):
        ProductionTask.objects.filter(pk=t.pk).update(queue_rank=rank)
    record(user, task, "update", {"task": [None, str(task)], "queue": [i + 1, j + 1]}, job=task.job)


@transaction.atomic
def plan(task: ProductionTask, *, user, day: date | None) -> None:
    """The day a Manager plans this task for, shown on the calendar."""
    if not user.has_perm(PLAN):
        raise PermissionDenied("Only Admin and Manager can plan tasks.")
    if task.status == TaskStatus.DONE:
        raise ValidationError("That task is done.")
    old = task.scheduled_date
    task.scheduled_date = day
    task.save(update_fields=["scheduled_date", "updated_at"])
    record(
        user,
        task,
        "update",
        {"task": [None, str(task)], "scheduled_date": [str(old or ""), str(day or "")]},
        job=task.job,
    )
