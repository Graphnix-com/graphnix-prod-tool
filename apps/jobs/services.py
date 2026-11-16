"""The only code that creates jobs and changes their status.

Every function here locks the job row, checks the state machine (`flow`) and the user's
permissions, then writes the job, one `JobStatusHistory` row and one `AuditLog` row in a
single transaction (FR-30, NFR-03).
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.services import record
from apps.sales.models import Inquiry

from . import flow, gates, workdays
from .flow import InvalidMove
from .models import HoldReason, Job, JobStatusHistory, Status

__all__ = [
    "InvalidMove",
    "actions_for",
    "cancel",
    "convert_inquiry",
    "create_job",
    "hold",
    "move",
    "release_hold",
    "reopen_cancelled",
    "reopen_mockup",
    "skip_mockup",
    "square_estimate_accepted",
    "square_estimate_job",
    "square_quote_sent",
    "store_order_job",
]


def _require(user, perm: str) -> None:
    if not user.has_perm(perm):
        raise PermissionDenied(f"You don't have permission to do that ({perm}).")


def _require_reason(note: str) -> str:
    note = (note or "").strip()
    if not note:
        raise InvalidMove("A reason is required for this move.")
    return note


def _locked(job: Job) -> Job:
    return Job.objects.select_for_update().get(pk=job.pk)


def _on_enter(job: Job, status: str, now) -> None:
    """Dates that follow from reaching a stage."""
    if status == Status.QUOTE_SENT and job.quote_sent_at is None:
        job.quote_sent_at = now
    elif status == Status.INVOICED and job.quote_approved_at is None:
        job.quote_approved_at = now  # the quote is approved once it's invoiced
    elif status == Status.MOCKUP_APPROVED:
        # The 14-business-day queue starts at each approval; a reopened mock-up gets a new
        # suggestion. The due date itself is only filled in if nobody has set one.
        job.mockup_approved_at = now
        job.suggested_due_date = workdays.calendar().add_business_days(
            timezone.localdate(now), workdays.turnaround_days()
        )
        if job.due_date is None:
            job.due_date = job.suggested_due_date
    elif status == Status.CLOSED:
        job.closed_at = now


def _change_status(
    job: Job, to_status: str, user, note: str, source: str, action: str, extra: dict | None = None
) -> Job:
    old = job.status
    now = timezone.now()
    job.status = to_status
    job.status_changed_at = now
    _on_enter(job, to_status, now)
    job.save()
    JobStatusHistory.objects.create(
        job=job,
        from_status=old,
        to_status=to_status,
        source=source,
        changed_by=user if user is not None and user.is_authenticated else None,
        note=note,
        changed_at=now,
    )
    changes = {"status": [old, to_status]}
    if note:
        changes["reason"] = [None, note]
    changes.update(extra or {})
    record(user, job, action, changes)
    return job


@transaction.atomic
def create_job(
    *,
    customer,
    title: str,
    start: str,
    user,
    square_number: str = "",
    inquiry: Inquiry | None = None,
    **fields,
) -> Job:
    """A new job at Quoting (quote first) or Invoiced (straight to invoice)."""
    _require(user, "jobs.add_job")
    if start not in flow.START_STATUSES:
        raise InvalidMove("A job starts at Quoting or at Invoiced.")
    square_number = (square_number or "").strip()
    if square_number and Job.objects.filter(square_number=square_number).exists():
        raise ValidationError({"square_number": f"Job {square_number} already exists."})
    job = Job(
        customer=customer,
        title=title,
        status=start,
        square_number=square_number,
        inquiry=inquiry,
        created_by=user,
        **fields,
    )
    job.save()
    label = "quote first" if start == Status.QUOTING else "straight to invoice"
    JobStatusHistory.objects.create(
        job=job,
        from_status="",
        to_status=start,
        changed_by=user,
        note=f"Job created ({label})",
        changed_at=job.status_changed_at,
    )
    record(user, job, "create", {"status": [None, start], "job_number": [None, job.job_number]})
    return job


@transaction.atomic
def convert_inquiry(
    inquiry: Inquiry, *, user, start: str, customer=None, title: str = "", square_number=""
) -> Job:
    """Turn an inquiry into a job (FR-02). The customer must be matched or created first."""
    inquiry = Inquiry.objects.select_for_update().get(pk=inquiry.pk)
    if inquiry.status in (Inquiry.Status.CONVERTED, Inquiry.Status.DECLINED):
        raise InvalidMove(f"This request is already {inquiry.get_status_display().lower()}.")
    customer = customer or inquiry.customer
    if customer is None:
        raise ValidationError({"customer": "Pick or create the customer first."})
    job = create_job(
        customer=customer,
        title=title or inquiry.subject or f"Request from {customer}",
        start=start,
        user=user,
        square_number=square_number,
        inquiry=inquiry,
        contact=inquiry.contact,
        source=inquiry.channel,
    )
    old_status = inquiry.status
    inquiry.status = Inquiry.Status.CONVERTED
    inquiry.customer = customer
    inquiry.save(update_fields=["status", "customer"])
    record(user, inquiry, "update", {"status": [old_status, inquiry.status], "job": [None, job.pk]})
    return job


@transaction.atomic
def move(job: Job, to_status: str, *, user, note: str = "", source: str = "user") -> Job:
    """Move a job along the path: one stage forward, or (with a reason) anywhere else."""
    job = _locked(job)
    kind = flow.classify(job.status, to_status)
    _require(user, flow.permission_for(kind, job.status))
    if kind == flow.JUMP:
        note = _require_reason(note)
    gate = gates.blocking(job, to_status, user)
    extra = None
    if gate:
        # FR-07: only an Admin, and only with a reason.
        if not user.has_perm(gates.OVERRIDE):
            raise InvalidMove(
                f"{gate}." if gate == gates.HIDDEN else f"{gate}. An Admin can override it."
            )
        if not (note or "").strip():
            raise InvalidMove(f"{gate}. Give a reason to move it anyway.")
        extra = {"payment_gate": [None, gate]}
    return _change_status(
        job,
        to_status,
        user,
        (note or "").strip(),
        source,
        "status_change" if kind == flow.FORWARD and not gate else "override",
        extra,
    )


@transaction.atomic
def square_invoiced(job: Job, *, note: str, event_id: str = "") -> Job:
    """A Square invoice for this job was sent (FR-06): a job still at the quote stages moves
    to Invoiced, with Square as the source. A job already past Invoiced stays where it is."""
    job = _locked(job)
    if job.status not in flow.BEFORE_INVOICED:
        return job
    if job.quote_approved_at is None:
        job.quote_approved_at = timezone.now()
    return _change_status(
        job,
        Status.INVOICED,
        None,
        note,
        JobStatusHistory.Source.SQUARE,
        "status_change",
        {"square_event": [None, event_id]} if event_id else None,
    )


def _system_job(
    *,
    customer,
    title: str,
    status: str,
    note: str,
    source="",
    fulfillment="",
    event_id="",
    square_number="",
) -> Job:
    """A job made by the tracker itself from something Square reported, already at
    `status`, with Square as the source of its first history row. `square_number` becomes
    the job's number unless another job already has it."""
    now = timezone.now()
    if square_number and Job.objects.filter(square_number=square_number).exists():
        square_number = ""
    job = Job(
        customer=customer,
        square_number=square_number,
        title=title[:200],
        status=status,
        status_changed_at=now,
        source=source,
        fulfillment_method=fulfillment,
    )
    _on_enter(job, status, now)
    job.save()
    JobStatusHistory.objects.create(
        job=job,
        from_status="",
        to_status=status,
        source=JobStatusHistory.Source.SQUARE,
        note=note,
        changed_at=now,
    )
    changes = {"status": [None, status], "job_number": [None, job.job_number]}
    if event_id:
        changes["square_event"] = [None, event_id]
    record(None, job, "create", changes)
    return job


@transaction.atomic
def store_order_job(
    *,
    customer,
    title: str,
    note: str,
    fulfillment: str = "",
    event_id: str = "",
    number: str = "",
) -> Job:
    """A job for an online store order: paid at checkout, with the store's design already
    approved, so it starts at Mock-Up Approved and its due date runs from now."""
    from apps.sales.models import Channel

    return _system_job(
        customer=customer,
        title=title,
        status=Status.MOCKUP_APPROVED,
        note=note,
        source=Channel.ONLINE_STORE,
        fulfillment=fulfillment,
        event_id=event_id,
        square_number=number,
    )


@transaction.atomic
def square_estimate_job(*, customer, title: str, note: str) -> Job:
    """A job for a Square estimate sent to a customer with no job waiting: at Quote Sent."""
    return _system_job(customer=customer, title=title, status=Status.QUOTE_SENT, note=note)


@transaction.atomic
def square_quote_sent(job: Job, *, note: str) -> Job:
    """A Square estimate was sent for this job: Quoting moves to Quote Sent."""
    job = _locked(job)
    if job.status != Status.QUOTING:
        return job
    return _change_status(
        job, Status.QUOTE_SENT, None, note, JobStatusHistory.Source.SQUARE, "status_change"
    )


@transaction.atomic
def square_estimate_accepted(job: Job, *, note: str) -> Job:
    """The customer accepted the Square estimate: Quoting or Quote Sent moves to Invoiced,
    since Square turns the estimate into an invoice at once. A job already further on stays
    where it is."""
    job = _locked(job)
    if job.status not in flow.BEFORE_INVOICED:
        if job.quote_approved_at is None and job.status == Status.INVOICED:
            Job.objects.filter(pk=job.pk).update(quote_approved_at=timezone.now())
        return job
    return _change_status(
        job, Status.INVOICED, None, note, JobStatusHistory.Source.SQUARE, "status_change"
    )


def skip_mockup(job: Job, *, user, note: str) -> Job:
    """Invoiced → Materials ordered, e.g. online store orders."""
    if job.status not in flow.SKIP_MOCKUP_FROM:
        raise InvalidMove("The mock-up can only be skipped from Invoiced.")
    return move(job, flow.SKIP_MOCKUP_TO, user=user, note=note)


def reopen_mockup(job: Job, *, user, note: str) -> Job:
    """Back to Invoiced after approval, to draw a new mock-up and send it again, e.g. the
    customer changed their mind."""
    if job.status not in flow.REOPEN_MOCKUP_FROM:
        raise InvalidMove(
            "A mock-up can be reopened from Mock-up approved, Materials ordered "
            "or Ready for production."
        )
    return move(job, flow.REOPEN_MOCKUP_TO, user=user, note=note)


@transaction.atomic
def hold(job: Job, *, user, reason_code: str, note: str = "") -> Job:
    job = _locked(job)
    _require(user, flow.OVERRIDE)
    if job.status not in flow.HOLDABLE:
        raise InvalidMove(f"A job at {job.get_status_display()} can't be put on hold.")
    if reason_code not in HoldReason.values:
        raise InvalidMove("Pick a reason for the hold.")
    note = (note or "").strip()
    if reason_code == HoldReason.OTHER:
        note = _require_reason(note)
    job.resume_status = job.status
    job.hold_reason_code = reason_code
    job.hold_reason = note
    label = HoldReason(reason_code).label
    return _change_status(
        job, Status.ON_HOLD, user, f"{label}: {note}" if note else label, "user", "override"
    )


@transaction.atomic
def release_hold(job: Job, *, user, note: str = "") -> Job:
    job = _locked(job)
    _require(user, flow.OVERRIDE)
    if job.status != Status.ON_HOLD:
        raise InvalidMove("The job isn't on hold.")
    to_status = job.resume_status
    job.resume_status = ""
    job.hold_reason_code = ""
    job.hold_reason = ""
    return _change_status(job, to_status, user, (note or "").strip(), "user", "override")


@transaction.atomic
def cancel(job: Job, *, user, note: str) -> Job:
    job = _locked(job)
    _require(user, flow.OVERRIDE)
    if job.status not in flow.CANCELLABLE:
        raise InvalidMove(f"A job at {job.get_status_display()} can't be cancelled.")
    note = (note or "").strip()  # optional
    if job.status != Status.ON_HOLD:
        job.resume_status = job.status
    job.hold_reason_code = ""
    job.hold_reason = ""
    job.cancel_reason = note
    # Step 4: cancelling also cancels the job's pending notifications (FR-36).
    return _change_status(job, Status.CANCELLED, user, note, "user", "override")


@transaction.atomic
def reopen_cancelled(job: Job, *, user, note: str) -> Job:
    """Admin only: back to the stage the job was cancelled from."""
    job = _locked(job)
    _require(user, flow.REOPEN)
    if job.status != Status.CANCELLED:
        raise InvalidMove("The job isn't cancelled.")
    note = _require_reason(note)
    to_status = job.resume_status
    job.resume_status = ""
    job.cancel_reason = ""
    return _change_status(job, to_status, user, note, "user", "override")


# --- What the job page offers ------------------------------------------------------


def actions_for(job: Job, user) -> dict:
    """The moves this user may make on this job now, for the job page's buttons."""
    status = job.status
    override = user.has_perm(flow.OVERRIDE)
    nxt = flow.next_status(status)
    forward = None
    if nxt is not None and status in flow.LANE and user.has_perm(flow.LANE[status]):
        gate = gates.blocking(job, nxt, user)
        forward = {
            "to": nxt,
            "label": Status(nxt).label,
            "gate": gate,
            "can_override": bool(gate) and user.has_perm(gates.OVERRIDE),
        }
    jump_targets = []
    if override and status in flow.PATH and status != Status.CLOSED:
        jump_targets = [
            {"to": s, "label": Status(s).label}
            for s in flow.PATH
            if s not in (status, nxt, Status.CLOSED)
        ]
    return {
        "forward": forward,
        "skip_mockup": override and status in flow.SKIP_MOCKUP_FROM,
        "reopen_mockup": override and status in flow.REOPEN_MOCKUP_FROM,
        "hold": override and status in flow.HOLDABLE,
        "release": override and status == Status.ON_HOLD,
        "cancel": override and status in flow.CANCELLABLE,
        "reopen": user.has_perm(flow.REOPEN) and status == Status.CANCELLED,
        "jump_targets": jump_targets,
        "hold_reasons": HoldReason.choices,
    }
