"""Due-date flags with a frozen clock (FR-34), using Edmonton's date, not UTC's (NFR-11)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.accounts.models import AppSetting
from apps.jobs.flags import AT_RISK, OVERDUE, Flagger
from apps.jobs.models import Status

pytestmark = pytest.mark.django_db
EDMONTON = ZoneInfo("America/Edmonton")


def freeze(monkeypatch, local: datetime):
    monkeypatch.setattr(timezone, "now", lambda: local.astimezone(ZoneInfo("UTC")))


@pytest.mark.parametrize(
    "today,due,want",
    [
        # Due Thu 19 Nov 2026; working days are Mon-Thu.
        (date(2026, 11, 11), date(2026, 11, 19), None),  # Wed: 5 working days out
        (date(2026, 11, 12), date(2026, 11, 19), AT_RISK),  # Thu: 16, 17, 18, 19 = 4
        (date(2026, 11, 13), date(2026, 11, 19), AT_RISK),  # Fri, a day off: still 4
        (date(2026, 11, 19), date(2026, 11, 19), AT_RISK),  # due today
        (date(2026, 11, 20), date(2026, 11, 19), OVERDUE),
    ],
)
def test_at_risk_window_counts_working_days(today, due, want, job_at):
    assert Flagger(on=today)(job_at(Status.MATERIALS_ORDERED, due_date=due)) == want


def test_in_production_is_never_at_risk_but_can_be_overdue(job_at):
    on = date(2026, 11, 18)
    assert Flagger(on=on)(job_at(Status.IN_PRODUCTION, due_date=date(2026, 11, 19))) is None
    assert Flagger(on=on)(job_at(Status.QA, due_date=date(2026, 11, 17))) == OVERDUE


def test_on_hold_counts_from_the_stage_it_left(job_at):
    on = date(2026, 11, 18)
    held_early = job_at(
        Status.ON_HOLD, resume_status=Status.MOCKUP_SENT, due_date=date(2026, 11, 19)
    )
    held_late = job_at(
        Status.ON_HOLD, resume_status=Status.IN_PRODUCTION, due_date=date(2026, 11, 19)
    )
    assert Flagger(on=on)(held_early) == AT_RISK
    assert Flagger(on=on)(held_late) is None


@pytest.mark.parametrize("status", [Status.SHIPPED, Status.CLOSED, Status.CANCELLED])
def test_finished_jobs_are_never_flagged(status, job_at):
    assert Flagger(on=date(2027, 1, 1))(job_at(status, due_date=date(2026, 11, 1))) is None


def test_no_due_date_no_flag(job_at):
    assert Flagger(on=date(2026, 11, 18))(job_at(Status.QUOTING)) is None


def test_window_comes_from_settings(job_at):
    AppSetting.objects.create(key="at_risk_working_days", value=2)
    job = job_at(Status.QUOTING, due_date=date(2026, 11, 19))
    assert Flagger(on=date(2026, 11, 12))(job) is None  # 4 working days out
    assert Flagger(on=date(2026, 11, 17))(job) == AT_RISK  # 18, 19


# --- Late evening in Edmonton is already tomorrow in UTC --------------------------


def test_overdue_uses_edmonton_date_before_the_dst_change(monkeypatch, job_at):
    job = job_at(Status.MATERIALS_ORDERED, due_date=date(2027, 3, 11))
    # Thu 11 Mar 2027, 23:30 MST = Fri 06:30 UTC: still the due date, not overdue.
    freeze(monkeypatch, datetime(2027, 3, 11, 23, 30, tzinfo=EDMONTON))
    assert Flagger()(job) == AT_RISK
    freeze(monkeypatch, datetime(2027, 3, 12, 0, 30, tzinfo=EDMONTON))
    assert Flagger()(job) == OVERDUE


def test_overdue_uses_edmonton_date_after_the_dst_change(monkeypatch, job_at):
    job = job_at(Status.MATERIALS_ORDERED, due_date=date(2027, 3, 15))
    # Clocks go forward on Sun 14 Mar 2027. Mon 15 Mar, 23:30 MDT = Tue 05:30 UTC.
    freeze(monkeypatch, datetime(2027, 3, 15, 23, 30, tzinfo=EDMONTON))
    assert timezone.now().date() == date(2027, 3, 16)  # UTC has moved on...
    assert Flagger()(job) == AT_RISK  # ...but in Edmonton it's still the due date
    freeze(monkeypatch, datetime(2027, 3, 16, 0, 5, tzinfo=EDMONTON))
    assert Flagger()(job) == OVERDUE


def test_at_risk_across_the_dst_weekend(monkeypatch, job_at):
    # Thu 11 Mar to Thu 18 Mar 2027 spans the change: 15, 16, 17, 18 = 4 working days.
    job = job_at(Status.MOCKUP_APPROVED, due_date=date(2027, 3, 18))
    freeze(monkeypatch, datetime(2027, 3, 11, 9, 0, tzinfo=EDMONTON))
    assert Flagger()(job) == AT_RISK
    freeze(monkeypatch, datetime(2027, 3, 10, 9, 0, tzinfo=EDMONTON))
    assert Flagger()(job) is None
