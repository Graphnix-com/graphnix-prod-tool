from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.jobs import assignment, mockups

from . import calendar, queues
from .models import ProductionTask, Station


def _thumbs(tasks) -> dict:
    """Each job's approved mock-up, so the floor sees what to print."""
    seen = {}
    for task in tasks:
        if task.job_id not in seen:
            seen[task.job_id] = mockups.approved(task.job)
        task.approved_mockup = seen[task.job_id]
    return seen


def _rows(request, tasks):
    _thumbs(tasks)
    allowed = {p.pk for p in assignment.assignable_people(request.user)}
    for task in tasks:
        task.can_take = assignment.can_change(request.user, task, allowed)
    return tasks


@login_required
@permission_required("production.view_station", raise_exception=True)
def floor(request):
    return render(request, "production/floor.html", {"stations": queues.stations()})


@login_required
@permission_required("production.view_station", raise_exception=True)
def station_queue(request, pk):
    station = get_object_or_404(Station, pk=pk)
    tasks = _rows(request, queues.queue(station))
    return render(
        request,
        "production/queue.html",
        {
            "station": station,
            "tasks": tasks,
            "can_plan": request.user.has_perm(queues.PLAN),
            "here": request.get_full_path(),
        },
    )


@login_required
@permission_required("jobs.view_job", raise_exception=True)
def my_tasks(request):
    found = queues.mine(request.user)
    _rows(request, found["own"] + found["unassigned"])
    return render(request, "production/mine.html", {**found, "here": request.get_full_path()})


@login_required
@require_POST
def task_move(request, pk):
    task = get_object_or_404(ProductionTask.objects.select_related("job"), pk=pk)
    try:
        queues.move(task, user=request.user, up=request.POST.get("dir") == "up")
    except PermissionDenied as exc:
        messages.error(request, str(exc))
    return redirect(reverse("production:queue", args=[task.station_id]))


@login_required
@require_POST
def task_plan(request, pk):
    task = get_object_or_404(ProductionTask.objects.select_related("job"), pk=pk)
    text = request.POST.get("day", "")
    try:
        day = date.fromisoformat(text) if text else None
        queues.plan(task, user=request.user, day=day)
    except ValueError:
        messages.error(request, "That isn't a date.")
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
    return redirect(reverse("production:queue", args=[task.station_id]))


@login_required
@permission_required("jobs.view_job", raise_exception=True)
def month_view(request):
    today = timezone.localdate()
    try:
        year, month_no = (int(p) for p in request.GET.get("month", "").split("-"))
        date(year, month_no, 1)
    except ValueError:
        year, month_no = today.year, today.month
    station = None
    if request.GET.get("station", "").isdigit():
        station = Station.objects.filter(pk=request.GET["station"]).first()
    before, after = calendar.neighbours(year, month_no)
    return render(
        request,
        "production/calendar.html",
        {
            "weeks": calendar.month(year, month_no, station=station),
            "first": date(year, month_no, 1),
            "before": before,
            "after": after,
            "today": today,
            "station": station,
            "stations": Station.objects.filter(is_active=True),
        },
    )
