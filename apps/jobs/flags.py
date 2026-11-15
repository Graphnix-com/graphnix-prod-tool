"""Due-date flags (FR-34), worked out from the date in Edmonton, never UTC (NFR-11).

- Overdue: past the due date and not yet Shipped.
- At risk: due within the at-risk window (default 4 working days, Mon-Thu) and not yet
  In production. A job on hold counts from the stage it left.
"""

from datetime import date

from django.utils import timezone

from . import workdays
from .flow import PATH
from .models import Job, Status

OVERDUE = "overdue"
AT_RISK = "at_risk"
LABELS = {OVERDUE: "Overdue", AT_RISK: "At risk"}

FINISHED = {Status.SHIPPED, Status.CLOSED, Status.CANCELLED}


def today() -> date:
    return timezone.localdate()


def _effective_status(job: Job) -> str:
    return job.resume_status if job.status == Status.ON_HOLD and job.resume_status else job.status


def due_flag(job: Job, on: date, calendar: workdays.WorkCalendar, window: int) -> str | None:
    if job.due_date is None or job.status in FINISHED:
        return None
    if job.due_date < on:
        return OVERDUE
    status = _effective_status(job)
    before_production = status in PATH and PATH.index(status) < PATH.index(Status.IN_PRODUCTION)
    if before_production and calendar.workdays_until(on, job.due_date) <= window:
        return AT_RISK
    return None


class Flagger:
    """Reads the settings once, for flagging many jobs (the board, the Home page)."""

    def __init__(self, on: date | None = None):
        self.on = on or today()
        self.calendar = workdays.calendar()
        self.window = workdays.at_risk_days()

    def __call__(self, job: Job) -> str | None:
        return due_flag(job, self.on, self.calendar, self.window)
