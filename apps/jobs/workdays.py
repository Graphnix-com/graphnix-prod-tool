"""Business days for the 14-day queue and the at-risk warning (FR-34).

Settings live in AppSetting so an Admin can change them without a deploy:
  working_days               weekday numbers, Monday = 0. Default Mon-Thu (store hours,
                             decided 7 Nov 2026).
  holidays                   ISO dates that are not worked, e.g. ["2026-12-25"].
  turnaround_business_days   default 14: the queue after mock-up approval.
  at_risk_working_days       default 4: warn this many working days before the due date.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from apps.accounts.models import get_setting

DEFAULT_WORKING_DAYS = [0, 1, 2, 3]
DEFAULT_TURNAROUND = 14
DEFAULT_AT_RISK = 4


@dataclass(frozen=True)
class WorkCalendar:
    working_days: frozenset[int] = frozenset(DEFAULT_WORKING_DAYS)
    holidays: frozenset[date] = field(default_factory=frozenset)

    def is_workday(self, day: date) -> bool:
        return day.weekday() in self.working_days and day not in self.holidays

    def add_business_days(self, start: date, days: int) -> date:
        """The date `days` working days after `start` (start itself is not counted)."""
        if not self.working_days:
            raise ValueError("No working days are set.")
        day, counted = start, 0
        while counted < days:
            day += timedelta(days=1)
            if self.is_workday(day):
                counted += 1
        return day

    def workdays_until(self, start: date, end: date) -> int:
        """Working days after `start` up to and including `end`; negative if `end` is past."""
        if end < start:
            return -self.workdays_until(end, start)
        return sum(
            1 for n in range(1, (end - start).days + 1) if self.is_workday(start + timedelta(n))
        )


def calendar() -> WorkCalendar:
    return WorkCalendar(
        working_days=frozenset(get_setting("working_days", DEFAULT_WORKING_DAYS)),
        holidays=frozenset(date.fromisoformat(d) for d in get_setting("holidays", [])),
    )


def turnaround_days() -> int:
    return int(get_setting("turnaround_business_days", DEFAULT_TURNAROUND))


def at_risk_days() -> int:
    return int(get_setting("at_risk_working_days", DEFAULT_AT_RISK))
