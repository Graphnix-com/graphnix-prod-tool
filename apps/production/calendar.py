"""The calendar (FR-35): job due dates, tasks a Manager planned for a day, and jobs ready for
pickup, by month. No money on it."""

from __future__ import annotations

import calendar as cal
from datetime import date, timedelta

from apps.jobs.flags import Flagger
from apps.jobs.models import Job, Status

from .models import ProductionTask, TaskStatus

CLOSED = (Status.CLOSED, Status.CANCELLED)


def month(year: int, month_no: int, *, station=None) -> list[list[dict]]:
    """Weeks (Monday first) of days: {day, in_month, jobs: [(job, flag)], tasks: [task]}."""
    weeks = cal.Calendar(firstweekday=0).monthdatescalendar(year, month_no)
    first, last = weeks[0][0], weeks[-1][-1]
    flagger = Flagger()
    jobs = (
        Job.objects.filter(due_date__range=(first, last))
        .exclude(status__in=CLOSED)
        .select_related("customer")
        .order_by("priority", "due_date")
    )
    if station is not None:
        jobs = jobs.filter(tasks__station=station).distinct()
    tasks = (
        ProductionTask.objects.filter(scheduled_date__range=(first, last))
        .exclude(status=TaskStatus.DONE)
        .exclude(job__status__in=CLOSED)
        .select_related("job", "decoration_method", "assigned_to", "station")
    )
    if station is not None:
        tasks = tasks.filter(station=station)
    by_day: dict[date, dict] = {}
    for job in jobs:
        by_day.setdefault(job.due_date, {"jobs": [], "tasks": []})["jobs"].append(
            (job, flagger(job))
        )
    for task in tasks:
        by_day.setdefault(task.scheduled_date, {"jobs": [], "tasks": []})["tasks"].append(task)
    return [
        [
            {
                "day": d,
                "in_month": d.month == month_no,
                **by_day.get(d, {"jobs": [], "tasks": []}),
            }
            for d in week
        ]
        for week in weeks
    ]


def neighbours(year: int, month_no: int) -> tuple[date, date]:
    first = date(year, month_no, 1)
    before = (first - timedelta(days=1)).replace(day=1)
    after = (first + timedelta(days=32)).replace(day=1)
    return before, after
