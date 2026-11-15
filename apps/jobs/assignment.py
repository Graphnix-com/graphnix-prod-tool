"""Who works on a task (decoration).

- Admin/Owner can assign a task to anyone.
- Manager can assign to anyone except Admin/Owner.
- Production can take a task for themselves, or let go of their own.

A change is allowed only if both the person it's moving from and the person it's moving to are
in the set the user may assign to. So a Manager can't take a task away from an Admin, and
Production can take an unassigned task but not one that's someone else's.

Assignment keeps working once the job is in production (unlike editing items and tasks).
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from apps.accounts import roles
from apps.audit.services import record
from apps.production.models import ProductionTask

from .models import Status

STAFF_ROLES = [roles.ADMIN, roles.MANAGER, roles.PRODUCTION]
CLOSED = {Status.CLOSED, Status.CANCELLED}


def assignable_people(user):
    """Everyone this user may assign a task to."""
    User = get_user_model()
    staff = User.objects.filter(is_active=True, groups__name__in=STAFF_ROLES).distinct()
    if user.has_perm("production.assign_any_task"):
        return staff
    if user.has_perm("production.assign_staff_task"):
        return staff.exclude(groups__name=roles.ADMIN).exclude(is_superuser=True)
    if user.has_perm("production.take_task"):
        return staff.filter(pk=user.pk)
    return User.objects.none()


def can_change(user, task, allowed_ids: set[int] | None = None) -> bool:
    """Whether this user may change who has this task at all."""
    if allowed_ids is None:
        allowed_ids = set(assignable_people(user).values_list("pk", flat=True))
    return bool(allowed_ids) and (task.assigned_to_id is None or task.assigned_to_id in allowed_ids)


@transaction.atomic
def assign(task: ProductionTask, assignee, *, user) -> ProductionTask:
    """Give the task to `assignee`, or to nobody if None."""
    task = (
        ProductionTask.objects.select_for_update(of=("self",))
        .select_related("job", "assigned_to")
        .get(pk=task.pk)
    )
    if task.job.status in CLOSED:
        raise ValidationError(f"The job is {task.job.get_status_display().lower()}.")
    allowed = set(assignable_people(user).values_list("pk", flat=True))
    if not can_change(user, task, allowed):
        raise PermissionDenied("You can't change who has this task.")
    if assignee is not None and assignee.pk not in allowed:
        raise PermissionDenied(f"You can't give tasks to {assignee}.")
    if task.assigned_to_id == (assignee.pk if assignee else None):
        return task
    before = str(task.assigned_to) if task.assigned_to else None
    task.assigned_to = assignee
    task.save(update_fields=["assigned_to", "updated_at"])
    record(
        user,
        task,
        "update",
        {
            "task": [str(task), str(task)],
            "assigned_to": [before, str(assignee) if assignee else None],
        },
        job=task.job,
    )
    return task
