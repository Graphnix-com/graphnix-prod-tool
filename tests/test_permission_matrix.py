"""The spec's role table, row by row, through the real pages (NFR-01).

Columns: Admin/Owner, Manager, Production, Production + QA and shipping, Production +
Customer entry (Avery has both extras).
"""

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.jobs.models import HoldReason, Job, Status

pytestmark = pytest.mark.django_db

PEOPLE = {
    "admin": [roles.ADMIN],
    "manager": [roles.MANAGER],
    "production": [roles.PRODUCTION],
    "qa": [roles.PRODUCTION, roles.QA_AND_SHIPPING],
    "entry": [roles.PRODUCTION, roles.CUSTOMER_ENTRY],
}
EVERYONE = set(PEOPLE)


@pytest.fixture
def as_role(client, django_user_model):
    def sign_in(role):
        user = django_user_model.objects.create_user(email=None, full_name=role.title())
        user.groups.add(*Group.objects.filter(name__in=PEOPLE[role]))
        # Admin/Owner gets the admin site from their Microsoft 365 group at sign-in.
        user.is_staff = role == "admin"
        user.save()
        client.force_login(user)
        return client

    return sign_in


def allowed(response) -> bool:
    return response.status_code == 200


def check_row(as_role, expected: set[str], probe):
    got = {role for role in PEOPLE if probe(as_role(role))}
    assert got == expected


def test_see_board_jobs_items_and_tasks(as_role, job_at):
    job = job_at(Status.IN_PRODUCTION)

    def probe(c):
        return allowed(c.get(reverse("jobs:list"))) and allowed(
            c.get(reverse("jobs:detail", args=[job.pk]))
        )

    check_row(as_role, EVERYONE, probe)


def test_see_prices_totals_and_payment_status(as_role, job_at):
    job = job_at(Status.INVOICED)
    url = reverse("jobs:detail", args=[job.pk]) + "?tab=details"
    check_row(as_role, {"admin", "manager"}, lambda c: "Payment" in c.get(url).content.decode())


def test_see_customer_contacts_production_sees_names_only(as_role, job_at):
    job = job_at(Status.QA)
    url = reverse("jobs:detail", args=[job.pk]) + "?tab=details"
    check_row(
        as_role,
        {"admin", "manager", "qa", "entry"},
        lambda c: "pat@example.com" in c.get(url).content.decode(),
    )
    page = as_role("production").get(url).content.decode()
    assert "Pat Doe" in page


def test_log_inquiries_and_create_customers(as_role):
    def probe(c):
        return allowed(c.get(reverse("sales:new"))) and allowed(c.get(reverse("customers:new")))

    check_row(as_role, {"admin", "manager", "entry"}, probe)


def test_convert_and_edit_jobs_items_and_tasks(as_role, job_at):
    job = job_at(Status.QUOTING)

    def probe(c):
        return allowed(c.get(reverse("jobs:edit", args=[job.pk]))) and allowed(
            c.get(reverse("jobs:item_add", args=[job.pk]))
        )

    check_row(as_role, {"admin", "manager"}, probe)


@pytest.mark.parametrize(
    ("start", "to", "expected"),
    [
        (Status.QUOTING, Status.QUOTE_SENT, {"admin", "manager"}),
        (Status.READY_FOR_PRODUCTION, Status.IN_PRODUCTION, EVERYONE),
        (Status.IN_PRODUCTION, Status.QA, EVERYONE),
        (Status.QA, Status.READY, {"admin", "manager", "qa"}),
        (Status.READY, Status.SHIPPED, {"admin", "manager", "qa"}),
    ],
)
def test_move_a_job_forward(as_role, job_at, start, to, expected):
    def probe(c):
        job = job_at(start)
        c.post(reverse("jobs:action", args=[job.pk]), {"action": "move", "to_status": to})
        return Job.objects.get(pk=job.pk).status == to

    check_row(as_role, expected, probe)


@pytest.mark.parametrize(
    "action",
    [
        {"action": "move", "to_status": Status.QUOTING, "note": "Customer changed the order"},
        {"action": "hold", "reason_code": HoldReason.PRODUCT_DELAY, "note": "Blanks late"},
        {"action": "cancel", "note": "Customer cancelled"},
    ],
    ids=["backward", "hold", "cancel"],
)
def test_move_backward_hold_and_cancel(as_role, job_at, action):
    def probe(c):
        job = job_at(Status.MATERIALS_ORDERED)
        c.post(reverse("jobs:action", args=[job.pk]), action)
        return Job.objects.get(pk=job.pk).status != Status.MATERIALS_ORDERED

    check_row(as_role, {"admin", "manager"}, probe)


def test_add_notes_to_a_job(as_role, job_at):
    def probe(c):
        job = job_at(Status.IN_PRODUCTION)
        c.post(reverse("jobs:note_add", args=[job.pk]), {"body": "Screen 3 re-burned"})
        return job.notes.exists()

    check_row(as_role, EVERYONE, probe)


def test_manage_methods_stations_staff_and_the_shop_floor_pc(as_role):
    def probe(c):
        return all(
            allowed(c.get(url))
            for url in (
                reverse("admin:production_decorationmethod_changelist"),
                reverse("admin:accounts_user_changelist"),
                reverse("shop_floor_setup"),
            )
        )

    check_row(as_role, {"admin"}, probe)


def test_read_the_audit_log(as_role):
    url = reverse("admin:audit_auditlog_changelist")
    check_row(as_role, {"admin"}, lambda c: allowed(c.get(url)))
