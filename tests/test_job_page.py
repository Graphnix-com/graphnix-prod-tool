"""The job page: items and sizes, tasks, notes, timeline and status buttons (FR-21, FR-32)."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.jobs import editing, services, timeline
from apps.jobs.editing import Locked
from apps.jobs.models import ONE_SIZE, JobItem, Status
from apps.production.models import DecorationMethod, ProductionTask

pytestmark = pytest.mark.django_db


def method(name):
    return DecorationMethod.objects.get(name=name)


def tee(job, user, **sizes):
    data = {"description": "Gildan 5000 tee", "color": "Black", "is_sized": True}
    return editing.save_item(job, user=user, data=data, sizes=sizes or {"M": 10, "L": 15})


# --- Items and sizes --------------------------------------------------------------


def test_sized_item_keeps_a_row_per_size(manager, job_at):
    item = tee(job_at(Status.QUOTING), manager, S=5, M=10, XL=2)
    assert {s.size: s.quantity for s in item.sizes.all()} == {"S": 5, "M": 10, "XL": 2}
    assert item.total_quantity == 17 and item.line_no == 1


def test_a_mug_line_stores_its_quantity_as_one_os_row(manager, job_at):
    item = editing.save_item(
        job_at(Status.QUOTING),
        user=manager,
        data={"description": "11 oz mug", "is_sized": False},
        sizes={ONE_SIZE: 36},
    )
    assert [(s.size, s.quantity) for s in item.sizes.all()] == [(ONE_SIZE, 36)]


def test_line_numbers_count_up(manager, job_at):
    job = job_at(Status.QUOTING)
    assert [tee(job, manager).line_no for _ in range(3)] == [1, 2, 3]


def test_item_needs_a_quantity(manager, job_at):
    with pytest.raises(ValidationError):
        editing.save_item(job_at(Status.QUOTING), user=manager, data={"description": "x"}, sizes={})


def test_item_form_through_the_page(client, manager, job_at):
    job = job_at(Status.INVOICED)
    client.force_login(manager)
    response = client.post(
        reverse("jobs:item_add", args=[job.pk]),
        {
            "description": "Bella 3001",
            "color": "Navy",
            "is_sized": "on",
            "size_M": "12",
            "size_2XL": "3",
            "other_sizes": "LT=2, 6XL:1",
        },
    )
    assert response.status_code == 302
    item = job.items.get()
    assert {s.size: s.quantity for s in item.sizes.all()} == {"M": 12, "2XL": 3, "LT": 2, "6XL": 1}


def test_item_form_rejects_unreadable_other_sizes(client, manager, job_at):
    job = job_at(Status.INVOICED)
    client.force_login(manager)
    response = client.post(
        reverse("jobs:item_add", args=[job.pk]),
        {"description": "Tee", "is_sized": "on", "other_sizes": "lots of mediums"},
    )
    assert response.status_code == 200 and "SIZE=QTY" in response.content.decode()
    assert not JobItem.objects.exists()


def test_editing_an_item_is_audited_with_its_size_changes(manager, job_at):
    job = job_at(Status.QUOTING)
    item = tee(job, manager, M=10)
    editing.save_item(
        job, user=manager, data={"description": "Gildan 5000 tee", "color": "Navy"},
        sizes={"M": 12}, item=item,
    )  # fmt: skip
    entry = AuditLog.objects.get(entity_type="jobs.jobitem", action="update")
    assert entry.job_id == job.pk
    assert entry.changes["color"] == ["Black", "Navy"]
    assert entry.changes["sizes"] == [{"M": 10}, {"M": 12}]


@pytest.mark.parametrize(
    "status,editable",
    [
        (Status.QUOTING, True),
        (Status.READY_FOR_PRODUCTION, True),
        (Status.IN_PRODUCTION, False),
        (Status.QA, False),
        (Status.CANCELLED, False),
    ],
)
def test_items_lock_once_in_production(status, editable, job_at):
    assert editing.items_editable(job_at(status)) is editable


def test_on_hold_counts_from_the_stage_it_left(job_at):
    assert editing.items_editable(job_at(Status.ON_HOLD, resume_status=Status.MOCKUP_SENT))
    assert not editing.items_editable(job_at(Status.ON_HOLD, resume_status=Status.QA))


def test_locked_job_refuses_item_and_task_changes(manager, job_at):
    job = job_at(Status.QUOTING)
    item = tee(job, manager)
    services.move(job, Status.IN_PRODUCTION, user=manager, note="testing the lock")
    job.refresh_from_db()
    with pytest.raises(Locked):
        tee(job, manager)
    with pytest.raises(Locked):
        editing.delete_item(item, user=manager)
    with pytest.raises(Locked):
        editing.save_task(
            job, user=manager, data={"job_item": item, "decoration_method": method("Embroidery")}
        )


def test_production_cannot_edit_items(make_user, job_at):
    with pytest.raises(PermissionDenied):
        tee(job_at(Status.QUOTING), make_user("production"))


# --- Tasks ------------------------------------------------------------------------


def test_a_three_method_job_makes_three_tasks_in_sequence(manager, job_at):
    job = job_at(Status.MOCKUP_APPROVED)
    shirts = tee(job, manager, M=20, L=30)
    caps = editing.save_item(
        job, user=manager, data={"description": "Richardson 112 cap", "is_sized": False},
        sizes={ONE_SIZE: 24},
    )  # fmt: skip
    for item, name, placement in [
        (shirts, "Screen print", "front"),
        (shirts, "Embroidery", "left sleeve"),
        (caps, "Heat press", "front panel"),
    ]:
        editing.save_task(
            job,
            user=manager,
            data={"job_item": item, "decoration_method": method(name), "placement": placement},
        )
    tasks = list(ProductionTask.objects.filter(job=job))
    assert [(t.sequence, t.decoration_method.name) for t in tasks] == [
        (1, "Screen print"),
        (2, "Embroidery"),
        (3, "Heat press"),
    ]
    assert [t.qty_planned for t in tasks] == [50, 50, 24]
    # Each lands in its method's station queue for Step 3.
    assert all(t.station.decoration_method == t.decoration_method for t in tasks)


def test_changing_sizes_updates_the_tasks_planned_quantity(manager, job_at):
    job = job_at(Status.QUOTING)
    item = tee(job, manager, M=10)
    task = editing.save_task(
        job, user=manager, data={"job_item": item, "decoration_method": method("Embroidery")}
    )
    editing.save_item(
        job, user=manager, data={"description": item.description}, sizes={"M": 25}, item=item
    )
    task.refresh_from_db()
    assert task.qty_planned == 25


def test_task_for_another_jobs_item_is_refused(manager, job_at):
    other_item = tee(job_at(Status.QUOTING), manager)
    with pytest.raises(ValidationError):
        editing.save_task(
            job_at(Status.QUOTING),
            user=manager,
            data={"job_item": other_item, "decoration_method": method("Laser")},
        )


def test_task_form_with_notes_and_supplier_tick(client, manager, job_at):
    job = job_at(Status.QUOTING)
    item = tee(job, manager)
    client.force_login(manager)
    response = client.post(
        reverse("jobs:task_add", args=[job.pk]),
        {
            "job_item": item.pk,
            "decoration_method": method("Screen print").pk,
            "placement": "front",
            "notes": "2 colours: white, red. 230 mesh.",
            "outsourced": "",
        },
    )
    assert response.status_code == 302
    task = ProductionTask.objects.get()
    assert task.notes == "2 colours: white, red. 230 mesh." and task.sequence == 1


def test_only_admin_can_add_a_method_from_the_task_form(client, admin_user, manager, job_at):
    job = job_at(Status.QUOTING)
    item = tee(job, manager)
    client.force_login(manager)
    assert "new_method" not in client.get(reverse("jobs:task_add", args=[job.pk])).content.decode()
    with pytest.raises(PermissionDenied):
        editing.add_method(user=manager, name="Sublimation")

    client.force_login(admin_user)
    client.post(
        reverse("jobs:task_add", args=[job.pk]),
        {"job_item": item.pk, "new_method": "Sublimation", "placement": "all over"},
    )
    task = ProductionTask.objects.get()
    assert task.decoration_method.name == "Sublimation"
    assert task.station.name == "Sublimation"


# --- Timeline ---------------------------------------------------------------------


def test_timeline_lists_moves_notes_and_edits_newest_first(manager, customer):
    job = services.create_job(
        customer=customer, title="Hoodies", start=Status.QUOTING, user=manager
    )
    item = tee(job, manager)
    services.move(job, Status.QUOTE_SENT, user=manager)
    editing.add_note(job, user=manager, body="Customer asked for a rush")
    editing.save_task(
        job, user=manager, data={"job_item": item, "decoration_method": method("Embroidery")}
    )
    editing.update_details(job, user=manager, data={"title": "Team hoodies"})

    titles = [e.title for e in timeline.build(job)]
    assert titles == [
        "Job details changed",
        "Task added: Embroidery",
        "Note",
        "Quoting → Quote Sent",
        "Item 1 added: Gildan 5000 tee",
        "Created at Quoting",
    ]
    details = timeline.build(job)[0].detail
    assert details == "Title: Hoodies → Team hoodies"


def test_timeline_keeps_removed_items(manager, job_at):
    job = job_at(Status.QUOTING)
    item = tee(job, manager)
    editing.delete_item(item, user=manager)
    titles = [e.title for e in timeline.build(job)]
    assert titles[0] == "Item 1 removed: Gildan 5000 tee"


# --- The page itself --------------------------------------------------------------


@pytest.mark.parametrize("tab", ["items", "timeline", "details"])
def test_job_page_tabs_open(tab, client, manager, job_at):
    job = job_at(Status.QUOTING)
    tee(job, manager)
    client.force_login(manager)
    assert client.get(reverse("jobs:detail", args=[job.pk]) + f"?tab={tab}").status_code == 200


def test_production_sees_the_job_without_money_or_contacts(client, make_user, job_at):
    job = job_at(Status.READY_FOR_PRODUCTION, shared_drive_path=r"\\NAS\Jobs\G-1042")
    client.force_login(make_user("production"))
    page = client.get(reverse("jobs:detail", args=[job.pk]) + "?tab=details").content.decode()
    assert "Payment" not in page
    assert "pat@example.com" not in page
    assert "Move to In Production" in page
    assert "Put on hold" not in page and "Edit details" not in page


def test_manager_sees_payment_and_contacts(client, manager, job_at):
    job = job_at(Status.QUOTING)
    client.force_login(manager)
    page = client.get(reverse("jobs:detail", args=[job.pk]) + "?tab=details").content.decode()
    assert "Payment" in page and "pat@example.com" in page


def test_buttons_follow_the_rules(manager, make_user, job_at):
    actions = services.actions_for(job_at(Status.INVOICED), manager)
    assert actions["forward"]["to"] == Status.MOCKUP_IN_PROGRESS
    assert actions["skip_mockup"] and actions["hold"] and actions["cancel"]
    assert not actions["reopen_mockup"] and not actions["release"]

    avery = services.actions_for(job_at(Status.QA), make_user("avery"))
    assert avery["forward"]["to"] == Status.READY
    assert not avery["hold"] and not avery["jump_targets"]

    production = services.actions_for(job_at(Status.QA), make_user("production"))
    assert production["forward"] is None


def test_move_button_posts_through_the_rules(client, manager, make_user, job_at):
    job = job_at(Status.QUOTING)
    client.force_login(manager)
    client.post(
        reverse("jobs:action", args=[job.pk]), {"action": "move", "to_status": "quote_sent"}
    )
    job.refresh_from_db()
    assert job.status == Status.QUOTE_SENT

    client.force_login(make_user("production"))
    response = client.post(
        reverse("jobs:action", args=[job.pk]),
        {"action": "move", "to_status": "invoiced"},
        follow=True,
    )
    assert "permission" in response.content.decode()
    job.refresh_from_db()
    assert job.status == Status.QUOTE_SENT


def test_hold_button_and_release(client, manager, job_at):
    job = job_at(Status.MATERIALS_ORDERED)
    client.force_login(manager)
    url = reverse("jobs:action", args=[job.pk])
    client.post(url, {"action": "hold", "reason_code": "product_delay"})
    job.refresh_from_db()
    assert job.status == Status.ON_HOLD
    client.post(url, {"action": "release"})
    job.refresh_from_db()
    assert job.status == Status.MATERIALS_ORDERED


def test_details_form_refuses_duplicate_square_number(client, manager, job_at):
    job_at(Status.QUOTING, square_number="1042")
    job = job_at(Status.QUOTING)
    client.force_login(manager)
    response = client.post(
        reverse("jobs:edit", args=[job.pk]),
        {"title": "x", "square_number": "1042", "priority": 2},
    )
    assert response.status_code == 200 and "already exists" in response.content.decode()


def test_note_from_production_staff(client, make_user, job_at):
    job = job_at(Status.IN_PRODUCTION)
    client.force_login(make_user("production"))
    client.post(reverse("jobs:note_add", args=[job.pk]), {"body": "Screen 3 torn, re-burning"})
    assert job.notes.get().body == "Screen 3 torn, re-burning"


def test_home_is_the_landing_page_for_managers_and_the_board_for_production(
    client, manager, make_user
):
    client.force_login(manager)
    assert client.get("/").status_code == 200
    client.force_login(make_user("production"))
    assert client.get("/")["Location"] == reverse("jobs:list")
