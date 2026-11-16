"""The job state machine (FR-30): every move, per role, plus holds, cancels and history."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AppSetting
from apps.audit.models import AuditLog
from apps.jobs import flow, services
from apps.jobs.models import HoldReason, JobStatusHistory, Status, WaitingOn
from apps.jobs.services import InvalidMove

pytestmark = pytest.mark.django_db
EDMONTON = ZoneInfo("America/Edmonton")

PATH = [
    Status.QUOTING,
    Status.QUOTE_SENT,
    Status.INVOICED,
    Status.MOCKUP_SENT,
    Status.MOCKUP_APPROVED,
    Status.MATERIALS_ORDERED,
    Status.READY_FOR_PRODUCTION,
    Status.IN_PRODUCTION,
    Status.QA,
    Status.READY,
    Status.SHIPPED,
    Status.CLOSED,
]

# Written out from the spec rather than imported from apps.jobs.flow, so the test checks the
# rules instead of repeating them. Who may move a job forward out of each lane:
OFFICE_LANE = set(PATH[:6])  # Quoting .. Materials Ordered
PRODUCTION_LANE = {Status.READY_FOR_PRODUCTION, Status.IN_PRODUCTION}
FINISH_LANE = {Status.QA, Status.READY, Status.SHIPPED}
FORWARD_ALLOWED = [
    (OFFICE_LANE, {"admin", "manager"}),
    (PRODUCTION_LANE, {"admin", "manager", "production", "avery"}),
    (FINISH_LANE, {"admin", "manager", "avery"}),
]
JUMP_ALLOWED = {"admin", "manager"}  # backward or skipping: with a reason


def expected(role: str, frm: str, to: str) -> str:
    """'ok', 'denied' (no permission) or 'invalid' (no one may)."""
    if frm == Status.CLOSED:
        return "invalid"
    if PATH.index(to) == PATH.index(frm) + 1:
        allowed = next(roles for lane, roles in FORWARD_ALLOWED if frm in lane)
        return "ok" if role in allowed else "denied"
    if to == Status.CLOSED:
        return "invalid"
    return "ok" if role in JUMP_ALLOWED else "denied"


@pytest.mark.parametrize("role", ["admin", "manager", "production", "avery", "customer_entry"])
def test_every_move_for_each_role(role, make_user, job_at):
    user = make_user(role)
    mismatches = []
    for frm in PATH:
        for to in PATH:
            if frm == to:
                continue
            job = job_at(frm)
            try:
                services.move(job, to, user=user, note="reason given")
                got = "ok"
            except PermissionDenied:
                got = "denied"
            except InvalidMove:
                got = "invalid"
            want = expected(role, frm, to)
            if got != want:
                mismatches.append(f"{frm} → {to}: got {got}, expected {want}")
    assert not mismatches, "\n".join(mismatches)


def test_backward_or_skip_needs_a_reason(manager, job_at):
    with pytest.raises(InvalidMove, match="reason"):
        services.move(job_at(Status.MOCKUP_SENT), Status.INVOICED, user=manager)
    with pytest.raises(InvalidMove, match="reason"):
        services.move(job_at(Status.QUOTING), Status.INVOICED, user=manager, note="   ")


def test_forward_needs_no_reason(manager, job_at):
    job = services.move(job_at(Status.QUOTING), Status.QUOTE_SENT, user=manager)
    assert job.status == Status.QUOTE_SENT


def test_each_move_writes_one_history_row_and_one_audit_row(manager, job_at):
    job = job_at(Status.QUOTING)
    services.move(job, Status.QUOTE_SENT, user=manager)
    history = JobStatusHistory.objects.filter(job=job)
    audit = AuditLog.objects.filter(entity_type="jobs.job", entity_id=job.pk)
    assert history.count() == 1 and audit.count() == 1
    row = history.get()
    assert (row.from_status, row.to_status, row.changed_by) == ("quoting", "quote_sent", manager)
    assert audit.get().changes == {"status": ["quoting", "quote_sent"]}
    assert audit.get().actor == manager

    services.move(job, Status.QUOTING, user=manager, note="Customer changed the quantity")
    assert history.count() == 2 and audit.count() == 2
    last = audit.order_by("-id").first()
    assert last.action == "override"
    assert last.changes["reason"] == [None, "Customer changed the quantity"]


def test_refused_move_writes_nothing(make_user, job_at):
    job = job_at(Status.QUOTING)
    with pytest.raises(PermissionDenied):
        services.move(job, Status.QUOTE_SENT, user=make_user("production"))
    job.refresh_from_db()
    assert job.status == Status.QUOTING
    assert not JobStatusHistory.objects.filter(job=job).exists()
    assert not AuditLog.objects.filter(entity_id=job.pk).exists()


def test_quote_dates_are_stamped(manager, job_at):
    job = services.move(job_at(Status.QUOTING), Status.QUOTE_SENT, user=manager)
    assert job.quote_sent_at is not None
    job = services.move(job, Status.INVOICED, user=manager)
    assert job.quote_approved_at is not None  # invoiced is approved


# --- Mock-ups and due dates ------------------------------------------------------


def _at(monkeypatch, local: datetime):
    monkeypatch.setattr(timezone, "now", lambda: local.astimezone(ZoneInfo("UTC")))


def test_mockup_approval_suggests_due_date_14_business_days_mon_to_thu(
    manager, job_at, monkeypatch
):
    # Thursday 12 Nov 2026, evening in Edmonton (already Friday in UTC).
    _at(monkeypatch, datetime(2026, 11, 12, 20, 0, tzinfo=EDMONTON))
    job = services.move(job_at(Status.MOCKUP_SENT), Status.MOCKUP_APPROVED, user=manager)
    # 14 Mon-Thu days after Thu 12 Nov: 16-19, 23-26, 30 Nov-3 Dec, 7-8 Dec.
    assert job.suggested_due_date == date(2026, 12, 8)
    assert job.due_date == date(2026, 12, 8)
    assert job.mockup_approved_at is not None


def test_mockup_approval_keeps_a_due_date_already_set(manager, job_at, monkeypatch):
    _at(monkeypatch, datetime(2026, 11, 12, 10, 0, tzinfo=EDMONTON))
    job = job_at(Status.MOCKUP_SENT, due_date=date(2026, 11, 30))
    job = services.move(job, Status.MOCKUP_APPROVED, user=manager)
    assert job.due_date == date(2026, 11, 30)
    assert job.suggested_due_date == date(2026, 12, 8)


def test_holidays_and_turnaround_come_from_settings(manager, job_at, monkeypatch):
    AppSetting.objects.create(key="holidays", value=["2026-11-23"])
    AppSetting.objects.create(key="turnaround_business_days", value=10)
    _at(monkeypatch, datetime(2026, 11, 12, 10, 0, tzinfo=EDMONTON))
    job = services.move(job_at(Status.MOCKUP_SENT), Status.MOCKUP_APPROVED, user=manager)
    # 10 days, skipping Mon 23 Nov: 16-19, 24-26, 30 Nov, 1-2 Dec.
    assert job.suggested_due_date == date(2026, 12, 2)


def test_skip_mockup_only_from_invoiced(manager, job_at):
    job = services.skip_mockup(job_at(Status.INVOICED), user=manager, note="Online store order")
    assert job.status == Status.MATERIALS_ORDERED
    with pytest.raises(InvalidMove):
        services.skip_mockup(job_at(Status.QUOTE_SENT), user=manager, note="x")


@pytest.mark.parametrize(
    "frm", [Status.MOCKUP_APPROVED, Status.MATERIALS_ORDERED, Status.READY_FOR_PRODUCTION]
)
def test_reopen_mockup(frm, manager, job_at):
    job = services.reopen_mockup(job_at(frm), user=manager, note="Customer wants navy ink")
    assert job.status == Status.INVOICED


def test_reopen_mockup_refused_elsewhere_and_for_production(make_user, manager, job_at):
    with pytest.raises(InvalidMove):
        services.reopen_mockup(job_at(Status.IN_PRODUCTION), user=manager, note="x")
    with pytest.raises(PermissionDenied):
        services.reopen_mockup(
            job_at(Status.MOCKUP_APPROVED), user=make_user("production"), note="x"
        )


# --- Hold, cancel, reopen --------------------------------------------------------


def test_hold_and_release_return_to_the_same_stage(manager, job_at):
    job = services.hold(
        job_at(Status.MATERIALS_ORDERED), user=manager, reason_code=HoldReason.PRODUCT_DELAY
    )
    assert job.status == Status.ON_HOLD
    assert job.resume_status == Status.MATERIALS_ORDERED
    assert job.history.last().note == "Product Delay"
    job = services.release_hold(job, user=manager)
    assert job.status == Status.MATERIALS_ORDERED
    assert job.resume_status == "" and job.hold_reason_code == ""


def test_hold_other_needs_a_note_and_shipped_cant_be_held(manager, job_at):
    with pytest.raises(InvalidMove, match="reason"):
        services.hold(job_at(Status.QA), user=manager, reason_code=HoldReason.OTHER)
    with pytest.raises(InvalidMove):
        services.hold(job_at(Status.SHIPPED), user=manager, reason_code=HoldReason.OTHER, note="x")


def test_production_cannot_hold_or_cancel(make_user, job_at):
    user = make_user("avery")
    with pytest.raises(PermissionDenied):
        services.hold(job_at(Status.QA), user=user, reason_code=HoldReason.PRODUCT_DELAY)
    with pytest.raises(PermissionDenied):
        services.cancel(job_at(Status.QA), user=user, note="x")


def test_cancel_from_hold_remembers_the_stage_before_the_hold(admin_user, manager, job_at):
    job = services.hold(
        job_at(Status.MOCKUP_SENT), user=manager, reason_code=HoldReason.MOCKUP_CHANGE
    )
    job = services.cancel(job, user=manager, note="Customer withdrew the order")
    assert job.status == Status.CANCELLED
    assert job.resume_status == Status.MOCKUP_SENT
    assert job.cancel_reason == "Customer withdrew the order"
    with pytest.raises(PermissionDenied):
        services.reopen_cancelled(job, user=manager, note="Back on")
    job = services.reopen_cancelled(job, user=admin_user, note="Customer came back")
    assert job.status == Status.MOCKUP_SENT


def test_cancel_without_a_reason_but_not_after_shipping(manager, job_at):
    job = services.cancel(job_at(Status.QUOTING), user=manager, note="  ")
    assert job.status == Status.CANCELLED and job.cancel_reason == ""
    assert job.history.get(to_status=Status.CANCELLED).note == ""
    with pytest.raises(InvalidMove):
        services.cancel(job_at(Status.SHIPPED), user=manager, note="x")


def test_move_refuses_side_states(manager, job_at):
    with pytest.raises(InvalidMove):
        services.move(job_at(Status.QUOTING), Status.ON_HOLD, user=manager, note="x")
    with pytest.raises(InvalidMove):
        services.move(job_at(Status.ON_HOLD), Status.QUOTING, user=manager, note="x")


def test_closing_stamps_closed_at(manager, job_at):
    job = services.move(job_at(Status.SHIPPED), Status.CLOSED, user=manager)
    assert job.closed_at is not None


# --- Waiting on (the Home page split) --------------------------------------------


@pytest.mark.parametrize(
    "status,hold_code,want",
    [
        (Status.QUOTE_SENT, "", WaitingOn.CUSTOMER),
        (Status.MOCKUP_SENT, "", WaitingOn.CUSTOMER),
        (Status.ON_HOLD, HoldReason.MOCKUP_CHANGE, WaitingOn.CUSTOMER),
        (Status.ON_HOLD, HoldReason.PRODUCT_DELAY, WaitingOn.GRAPHNIX),
        (Status.QUOTING, "", WaitingOn.GRAPHNIX),
        (Status.IN_PRODUCTION, "", WaitingOn.GRAPHNIX),
        (Status.SHIPPED, "", None),
        (Status.CANCELLED, "", None),
    ],
)
def test_waiting_on(status, hold_code, want, job_at):
    assert job_at(status, hold_reason_code=hold_code).waiting_on == want


def test_cancel_from_the_job_page_without_a_reason(client, manager, job_at):
    job = job_at(Status.QUOTING)
    client.force_login(manager)
    page = client.get(reverse("jobs:detail", args=[job.pk])).content.decode()
    form = page.split('value="cancel">')[1].split("</form>")[0]
    assert "required" not in form
    client.post(reverse("jobs:action", args=[job.pk]), {"action": "cancel", "note": ""})
    job.refresh_from_db()
    assert job.status == Status.CANCELLED


# --- Quote Approved, merged into Invoiced ----------------------------------------------


def test_quote_approved_is_no_longer_a_stage(manager, job_at):
    job = job_at(Status.QUOTE_SENT)
    assert Status.QUOTE_APPROVED not in flow.PATH
    with pytest.raises(InvalidMove):
        services.move(job, Status.QUOTE_APPROVED, user=manager)


def test_jobs_at_quote_approved_move_to_invoiced(job_at):
    from importlib import import_module

    from django.apps import apps

    merge = import_module("apps.jobs.migrations.0008_merge_quote_approved_into_invoiced")
    waiting = job_at(Status.QUOTE_APPROVED)
    held = job_at(Status.ON_HOLD, resume_status=Status.QUOTE_APPROVED)
    merge.forward(apps, None)
    waiting.refresh_from_db()
    held.refresh_from_db()
    assert waiting.status == Status.INVOICED and waiting.quote_approved_at is not None
    row = waiting.history.get()
    assert (row.from_status, row.to_status) == (Status.QUOTE_APPROVED, Status.INVOICED)
    assert row.get_from_status_display() == "Quote Approved"  # old history still reads
    assert held.status == Status.ON_HOLD and held.resume_status == Status.INVOICED


# --- Mock-up In Progress, retired ------------------------------------------------------


def test_invoiced_goes_straight_to_mockup_sent(manager, job_at):
    assert Status.MOCKUP_IN_PROGRESS not in flow.PATH
    job = services.move(job_at(Status.INVOICED), Status.MOCKUP_SENT, user=manager)
    assert job.status == Status.MOCKUP_SENT  # one step forward: no reason needed


def test_jobs_at_mockup_in_progress_move_to_invoiced(job_at):
    from importlib import import_module

    from django.apps import apps

    retire = import_module("apps.jobs.migrations.0009_retire_mockup_in_progress")
    drawing = job_at(Status.MOCKUP_IN_PROGRESS)
    held = job_at(Status.ON_HOLD, resume_status=Status.MOCKUP_IN_PROGRESS)
    retire.forward(apps, None)
    drawing.refresh_from_db()
    held.refresh_from_db()
    assert drawing.status == Status.INVOICED
    assert drawing.history.get().get_from_status_display() == "Mock-up In Progress"
    assert held.resume_status == Status.INVOICED
