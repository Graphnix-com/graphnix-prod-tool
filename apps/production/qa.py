"""The QA check (FR-26): one pass or fail by a Manager, no checklist (decided 17 Nov 2026).

- Pass: QA → Ready.
- Fail: a reason, and the tasks to redo; they go back to their queues as Rework (at the
  top), and the job back to In production.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.jobs import services as job_services
from apps.jobs.models import Job, Status

from .models import QACheck, TaskStatus

RUN = "production.run_qa_check"


def _check(job: Job, user) -> None:
    if not user.has_perm(RUN):
        raise PermissionDenied("Only Managers run the QA check.")
    if job.status != Status.QA:
        raise ValidationError(f"{job.display_number} isn't at QA.")


@transaction.atomic
def passed(job: Job, *, user, notes: str = "") -> Job:
    _check(job, user)
    notes = (notes or "").strip()
    QACheck.objects.create(job=job, result=QACheck.Result.PASS, notes=notes, reviewed_by=user)
    note = "QA passed" + (f": {notes}" if notes else "")
    return job_services.move(job, Status.READY, user=user, note=note, qa_passed=True)


@transaction.atomic
def failed(job: Job, *, user, reason: str, task_ids=()) -> Job:
    _check(job, user)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Say what failed.")
    tasks = list(job.tasks.filter(pk__in=[int(t) for t in task_ids if str(t).isdigit()]))
    for task in tasks:
        task.status = TaskStatus.REWORK
        task.rework_reason = reason[:1000]
        task.completed_at = None
        task.save(update_fields=["status", "rework_reason", "completed_at", "updated_at"])
    QACheck.objects.create(
        job=job,
        result=QACheck.Result.FAIL,
        notes=reason,
        rework_task_ids=[t.pk for t in tasks],
        reviewed_by=user,
    )
    redo = f" Rework: {', '.join(str(t) for t in tasks)}." if tasks else ""
    return job_services.move(
        job, Status.IN_PRODUCTION, user=user, note=f"QA failed: {reason}.{redo}"
    )
