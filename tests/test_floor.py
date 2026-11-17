"""Station queues, My tasks, QA, pickup and shipping, closing, the calendar (Step 3, PR 3)."""

from datetime import date, timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AppSetting
from apps.customers.models import Address
from apps.jobs import fulfilment, services
from apps.jobs.models import Job, JobItem, JobItemSize, Shipment, Status
from apps.jobs.services import InvalidMove
from apps.production import calendar, qa, queues
from apps.production.models import DecorationMethod, ProductionTask, QACheck, TaskStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def screen():
    method, _ = DecorationMethod.objects.get_or_create(name="Screen print", defaults={"code": "SP"})
    return method


def task_for(job, method, status=TaskStatus.QUEUED, **fields):
    item = JobItem.objects.create(job=job, description="Gildan 5000 tee", color="Black")
    JobItemSize.objects.create(job_item=item, size="L", quantity=20)
    return ProductionTask.objects.create(
        job=job, job_item=item, decoration_method=method, status=status, **fields
    )


def released(job_at, method, **job_fields):
    job = job_at(Status.READY_FOR_PRODUCTION, **job_fields)
    return job, task_for(job, method)


# --- Queues ---------------------------------------------------------------------------------


def test_release_puts_planned_tasks_in_the_queue(manager, job_at, screen):
    job = job_at(Status.MATERIALS_ORDERED)
    task = task_for(job, screen, status=TaskStatus.BLOCKED)
    services.system_move(job, Status.READY_FOR_PRODUCTION, note="All materials in")
    task.refresh_from_db()
    assert task.status == TaskStatus.QUEUED
    assert queues.queue(task.station) == [task]


def test_the_queue_holds_released_unfinished_tasks_only(job_at, screen):
    _, waiting = released(job_at, screen)
    task_for(job_at(Status.MATERIALS_ORDERED), screen)  # not released
    finished, _ = released(job_at, screen)
    ProductionTask.objects.filter(job=finished).update(status=TaskStatus.DONE)
    outsourced, _ = released(job_at, screen)
    ProductionTask.objects.filter(job=outsourced).update(outsourced=True)
    assert queues.queue(waiting.station) == [waiting]


def test_queue_order_rework_then_sooner_then_due_date(job_at, screen):
    today = timezone.localdate()
    _, later = released(job_at, screen, due_date=today + timedelta(days=9))
    _, sooner_job = released(job_at, screen, due_date=today + timedelta(days=20), priority=1)
    _, earlier = released(job_at, screen, due_date=today + timedelta(days=2))
    _, redo = released(job_at, screen, due_date=today + timedelta(days=30))
    redo.status = TaskStatus.REWORK
    redo.save()
    assert queues.queue(later.station) == [redo, sooner_job, earlier, later]


def test_a_manager_reorders_and_it_sticks(manager, make_user, job_at, screen):
    today = timezone.localdate()
    _, first = released(job_at, screen, due_date=today + timedelta(days=1))
    _, second = released(job_at, screen, due_date=today + timedelta(days=2))
    queues.move(second, user=manager, up=True)
    assert queues.queue(first.station) == [second, first]
    with pytest.raises(PermissionDenied):
        queues.move(first, user=make_user("production"), up=True)


def test_a_manager_plans_a_day_and_the_calendar_shows_it(manager, job_at, screen):
    job, task = released(job_at, screen, due_date=date(2026, 11, 27))
    queues.plan(task, user=manager, day=date(2026, 11, 24))
    weeks = calendar.month(2026, 11)
    days = {cell["day"]: cell for week in weeks for cell in week}
    assert days[date(2026, 11, 24)]["tasks"] == [task]
    assert days[date(2026, 11, 27)]["jobs"][0][0] == job


def test_the_station_page_shows_the_work_and_no_money(client, make_user, job_at, screen):
    job, task = released(job_at, screen)
    client.force_login(make_user("production"))
    html = client.get(reverse("production:queue", args=[task.station_id])).content.decode()
    assert job.display_number in html and "Gildan 5000 tee" in html and "L 20" in html
    assert "$" not in html and "↑" not in html  # no reordering for the floor
    assert "Take" in html


def test_my_tasks_and_take(client, make_user, job_at, screen):
    floor = make_user("production")
    _, mine = released(job_at, screen)
    mine.assigned_to = floor
    mine.save()
    _, open_task = released(job_at, screen)
    floor.default_station = open_task.station
    floor.save()
    client.force_login(floor)
    url = reverse("production:mine")
    html = client.get(url).content.decode()
    assert html.index(mine.job.display_number) < html.index("Unassigned at")
    assert open_task.job.display_number in html
    response = client.post(
        reverse("jobs:task_assign", args=[open_task.job_id, open_task.pk]),
        {"assignee": "me", "next": url},
    )
    assert response["Location"] == url
    open_task.refresh_from_db()
    assert open_task.assigned_to == floor


def test_the_floor_and_calendar_pages_render(client, manager, job_at, screen):
    job, task = released(job_at, screen, due_date=timezone.localdate())
    client.force_login(manager)
    assert "Screen print" in client.get(reverse("production:floor")).content.decode()
    html = client.get(reverse("production:calendar")).content.decode()
    assert job.display_number in html
    filtered = reverse("production:calendar") + f"?station={task.station_id}"
    assert client.get(filtered).status_code == 200


# --- QA --------------------------------------------------------------------------------------


def test_qa_pass_moves_to_ready(manager, job_at):
    job = qa.passed(job_at(Status.QA), user=manager, notes="Looks great")
    assert job.status == Status.READY
    check = QACheck.objects.get()
    assert check.result == QACheck.Result.PASS and check.reviewed_by == manager
    assert job.history.get().note == "QA passed: Looks great"


def test_qa_fail_sends_tasks_back_as_rework(manager, job_at, screen):
    job = job_at(Status.QA)
    bad = task_for(job, screen, status=TaskStatus.DONE)
    good = task_for(job, screen, status=TaskStatus.DONE)
    with pytest.raises(ValidationError, match="Say what failed"):
        qa.failed(job, user=manager, reason=" ")
    job = qa.failed(job, user=manager, reason="Logo off-centre", task_ids=[bad.pk])
    bad.refresh_from_db()
    good.refresh_from_db()
    assert job.status == Status.IN_PRODUCTION
    assert bad.status == TaskStatus.REWORK and bad.rework_reason == "Logo off-centre"
    assert good.status == TaskStatus.DONE
    assert QACheck.objects.get().rework_task_ids == [bad.pk]
    assert queues.queue(bad.station) == [bad]


@pytest.mark.parametrize("role", ["avery", "production"])
def test_only_managers_run_qa(role, make_user, job_at):
    with pytest.raises(PermissionDenied):
        qa.passed(job_at(Status.QA), user=make_user(role))


def test_qa_to_ready_only_through_the_check(manager, job_at):
    with pytest.raises(InvalidMove, match="QA check"):
        services.move(job_at(Status.QA), Status.READY, user=manager)


def test_qa_through_the_page(client, manager, job_at):
    job = job_at(Status.QA)
    client.force_login(manager)
    html = client.get(reverse("jobs:detail", args=[job.pk])).content.decode()
    assert "Pass: move to Ready" in html
    client.post(reverse("jobs:action", args=[job.pk]), {"action": "qa_pass"})
    job.refresh_from_db()
    assert job.status == Status.READY


# --- Pickup and shipping ------------------------------------------------------------------------


def test_a_pickup_needs_who_collected(make_user, job_at):
    avery = make_user("avery")
    job = job_at(Status.READY)
    with pytest.raises(ValidationError, match="Who collected"):
        fulfilment.record_shipment(job, user=avery, data={"method": "pickup"})
    fulfilment.record_shipment(job, user=avery, data={"method": "pickup", "picked_up_by": "Pat"})
    job.refresh_from_db()
    assert job.status == Status.SHIPPED and job.history.get().note == "Picked up by Pat"


def test_a_courier_shipment(manager, job_at):
    job = job_at(Status.READY)
    with pytest.raises(ValidationError, match="Which courier"):
        fulfilment.record_shipment(job, user=manager, data={"method": "courier"})
    with pytest.raises(ValidationError, match="Name the courier"):
        fulfilment.record_shipment(
            job, user=manager, data={"method": "courier", "carrier": "other"}
        )
    shipment = fulfilment.record_shipment(
        job,
        user=manager,
        data={"method": "courier", "carrier": "purolator", "tracking_number": "PUR123"},
    )
    assert shipment.summary == "Shipped by Purolator, tracking PUR123"
    assert Shipment.objects.count() == 1  # the refused tries saved nothing


def test_shipped_only_through_the_form(manager, job_at):
    with pytest.raises(InvalidMove, match="Record the pickup or shipment"):
        services.move(job_at(Status.READY), Status.SHIPPED, user=manager)


def test_the_form_offers_the_customers_address(client, manager, job_at, customer):
    Address.objects.create(
        customer=customer,
        kind="shipping",
        line1="12 Main St",
        city="Calgary",
        province="AB",
        postal_code="T2P 1A1",
    )
    job = job_at(Status.READY, fulfillment_method="ship")
    client.force_login(manager)
    html = client.get(reverse("jobs:detail", args=[job.pk])).content.decode()
    assert "12 Main St" in html and "Canada Post" in html and "Save and move to Shipped" in html


def test_production_cant_record_a_shipment(make_user, job_at):
    with pytest.raises(PermissionDenied):
        fulfilment.record_shipment(
            job_at(Status.READY), user=make_user("production"), data={"method": "delivery"}
        )


# --- Closing ---------------------------------------------------------------------------


def test_shipped_jobs_close_after_14_days(job_at):
    now = timezone.now()
    old = job_at(Status.SHIPPED, status_changed_at=now - timedelta(days=15))
    recent = job_at(Status.SHIPPED, status_changed_at=now - timedelta(days=3))
    assert fulfilment.close_shipped(now) == [old]
    old.refresh_from_db()
    recent.refresh_from_db()
    assert old.status == Status.CLOSED and old.closed_at is not None
    assert old.history.get().note == "Closed 14 days after shipping"
    assert recent.status == Status.SHIPPED


def test_the_close_delay_is_a_setting(job_at):
    AppSetting.objects.create(key=fulfilment.CLOSE_SETTING, value=2)
    job = job_at(Status.SHIPPED, status_changed_at=timezone.now() - timedelta(days=3))
    fulfilment.close_shipped()
    assert Job.objects.get(pk=job.pk).status == Status.CLOSED
