"""Assigning a task (decoration) to someone.

Admin/Owner: anyone. Manager: anyone but Admin/Owner. Production: only themselves.
"""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.jobs import assignment, timeline
from apps.jobs.models import JobItem, Status
from apps.production.models import DecorationMethod, ProductionTask

pytestmark = pytest.mark.django_db


@pytest.fixture
def people(make_user):
    return {
        "jenn": make_user("admin", "Jenn"),
        "nick": make_user("admin", "Nick"),
        "mckenna": make_user("manager", "McKenna"),
        "rommel": make_user("production", "Rommel"),
        "isaac": make_user("production", "Isaac"),
        "avery": make_user("avery", "Avery"),
        "entry_only": make_user("customer_entry", "Front Desk"),
    }


@pytest.fixture
def task(job_at):
    job = job_at(Status.IN_PRODUCTION)  # assignment still works once in production
    item = JobItem.objects.create(job=job, description="Gildan 5000")
    return ProductionTask.objects.create(
        job=job, job_item=item, decoration_method=DecorationMethod.objects.get(code="screen-print")
    )


def names(user):
    return {p.full_name for p in assignment.assignable_people(user)}


def test_admin_can_assign_to_anyone(people):
    everyone = {"Jenn", "Nick", "McKenna", "Rommel", "Isaac", "Avery"}
    assert names(people["jenn"]) == everyone
    assert names(people["nick"]) == everyone


def test_manager_can_assign_to_anyone_but_admins(people):
    assert names(people["mckenna"]) == {"McKenna", "Rommel", "Isaac", "Avery"}


def test_production_can_only_assign_themselves(people):
    assert names(people["rommel"]) == {"Rommel"}
    assert names(people["avery"]) == {"Avery"}


def test_customer_entry_alone_cannot_assign(people):
    assert names(people["entry_only"]) == set()


def test_switched_off_staff_are_not_offered(people):
    people["isaac"].is_active = False
    people["isaac"].save()
    assert "Isaac" not in names(people["jenn"])


def test_admin_assigns_and_it_is_on_the_timeline(people, task):
    assignment.assign(task, people["rommel"], user=people["jenn"])
    task.refresh_from_db()
    assert task.assigned_to == people["rommel"]
    entry = AuditLog.objects.get(entity_type="production.productiontask", action="update")
    assert entry.actor == people["jenn"] and entry.job_id == task.job_id
    shown = timeline.build(task.job)[0]
    assert (
        shown.title.startswith("Task changed") and "Assigned to: (blank) → Rommel" in shown.detail
    )


def test_manager_cannot_give_a_task_to_an_admin(people, task):
    with pytest.raises(PermissionDenied):
        assignment.assign(task, people["nick"], user=people["mckenna"])


def test_manager_cannot_take_a_task_away_from_an_admin(people, task):
    assignment.assign(task, people["nick"], user=people["jenn"])
    with pytest.raises(PermissionDenied):
        assignment.assign(task, people["rommel"], user=people["mckenna"])


def test_manager_reassigns_between_production_staff(people, task):
    assignment.assign(task, people["rommel"], user=people["mckenna"])
    assignment.assign(task, people["isaac"], user=people["mckenna"])
    assignment.assign(task, None, user=people["mckenna"])
    task.refresh_from_db()
    assert task.assigned_to is None


def test_production_takes_an_open_task_and_lets_it_go(people, task):
    assignment.assign(task, people["rommel"], user=people["rommel"])
    assignment.assign(task, None, user=people["rommel"])
    task.refresh_from_db()
    assert task.assigned_to is None


def test_production_cannot_assign_someone_else(people, task):
    with pytest.raises(PermissionDenied):
        assignment.assign(task, people["isaac"], user=people["rommel"])


def test_production_cannot_take_someone_elses_task(people, task):
    assignment.assign(task, people["isaac"], user=people["mckenna"])
    with pytest.raises(PermissionDenied):
        assignment.assign(task, people["rommel"], user=people["rommel"])
    with pytest.raises(PermissionDenied):
        assignment.assign(task, None, user=people["rommel"])


def test_closed_and_cancelled_jobs_cannot_change(people, task):
    for status in (Status.CLOSED, Status.CANCELLED):
        task.job.status = status
        task.job.save()
        with pytest.raises(ValidationError):
            assignment.assign(task, people["rommel"], user=people["jenn"])


def test_assigning_the_same_person_again_writes_nothing(people, task):
    assignment.assign(task, people["rommel"], user=people["jenn"])
    assignment.assign(task, people["rommel"], user=people["jenn"])
    assert AuditLog.objects.filter(entity_type="production.productiontask").count() == 1


# --- The pages --------------------------------------------------------------------


def job_page(client, task):
    return client.get(reverse("jobs:detail", args=[task.job.pk])).content.decode()


def assign_url(task):
    return reverse("jobs:task_assign", args=[task.job.pk, task.pk])


def test_manager_sees_a_list_without_admins(client, people, task):
    client.force_login(people["mckenna"])
    page = job_page(client, task)
    assert 'name="assignee"' in page and ">Rommel<" in page
    assert ">Jenn<" not in page and ">Nick<" not in page


def test_production_sees_take_it_then_let_go(client, people, task):
    client.force_login(people["rommel"])
    assert "Take it" in job_page(client, task)
    client.post(assign_url(task), {"assignee": "me"})
    task.refresh_from_db()
    assert task.assigned_to == people["rommel"]
    page = job_page(client, task)
    assert "Let go" in page and "<select" not in page


def test_production_sees_a_colleagues_name_without_a_button(client, people, task):
    task.assigned_to = people["isaac"]
    task.save()
    client.force_login(people["rommel"])
    page = job_page(client, task)
    assert "Isaac" in page and "Take it" not in page and "Let go" not in page


def test_a_refused_assignment_shows_why(client, people, task):
    client.force_login(people["mckenna"])
    page = client.post(assign_url(task), {"assignee": str(people["jenn"].pk)}, follow=True)
    assert "can&#x27;t give tasks to Jenn" in page.content.decode()
    task.refresh_from_db()
    assert task.assigned_to is None


def test_the_board_card_shows_who_has_the_work(client, people, task):
    assignment.assign(task, people["rommel"], user=people["jenn"])
    client.force_login(people["isaac"])
    page = client.get(reverse("jobs:list")).content.decode()
    assert '<span class="who">Rommel</span>' in page
