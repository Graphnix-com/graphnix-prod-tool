"""Mock-up versions and their approval by email (Step 3, PR 1; FR-10 to FR-12)."""

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import AppSetting
from apps.audit.models import AuditLog
from apps.jobs import files, mockups, services
from apps.jobs.models import JobFile, MockupApproval, Status
from apps.jobs.services import InvalidMove

from .conftest import approval_for, mockup_version

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def png(name="mockup.png"):
    return SimpleUploadedFile(name, b"\x89PNG fake", content_type="image/png")


def send(job, user, version):
    return services.move(job, Status.MOCKUP_SENT, user=user, mockup=version.pk)


def approve(job, user, version, **kw):
    data = {"approved_by": "Pat Doe", "ticked": mockups.checks(), **kw}
    return services.move(job, Status.MOCKUP_APPROVED, user=user, mockup=version.pk, approval=data)


# --- Versions -----------------------------------------------------------------------


def test_versions_number_themselves(manager, job_at):
    job = job_at(Status.INVOICED)
    v1 = mockups.add_version(job, user=manager, upload=png(), note="Navy ink")
    on_job = files.add(job, [png("second.pdf")], user=manager)[0]
    v2 = mockups.add_version(job, user=manager, file=on_job)
    assert (v1.version, v2.version) == (1, 2) and v1.note == "Navy ink"
    assert v1.file.name == "mockup.png" and job.files.count() == 2
    assert AuditLog.objects.filter(entity_type="jobs.mockupversion").count() == 2


def test_a_mockup_must_be_an_image_or_pdf(manager, job_at):
    job = job_at(Status.INVOICED)
    with pytest.raises(Exception, match="image .* or a PDF"):
        mockups.add_version(job, user=manager, upload=SimpleUploadedFile("art.ai", b"x"))


def test_production_cant_add_versions(make_user, job_at):
    with pytest.raises(PermissionDenied):
        mockups.add_version(job_at(Status.INVOICED), user=make_user("production"), upload=png())


def test_a_mockups_file_cant_be_removed(manager, job_at):
    job = job_at(Status.INVOICED)
    version = mockup_version(job)
    with pytest.raises(PermissionDenied, match="mock-up v1"):
        files.remove(version.file, user=manager)
    assert JobFile.objects.filter(pk=version.file_id).exists()


# --- Mock-up Pending ----------------------------------------------------------------------


def test_mockup_sent_names_the_version(manager, job_at):
    job = job_at(Status.INVOICED)
    version = mockup_version(job)
    job = send(job, manager, version)
    version.refresh_from_db()
    assert job.status == Status.MOCKUP_SENT and version.sent_at == job.status_changed_at
    assert job.history.get().note == "Mock-up v1 sent"


def test_mockup_sent_needs_a_version(manager, job_at):
    job = job_at(Status.INVOICED)
    with pytest.raises(InvalidMove, match="Add the mock-up as a version first"):
        services.move(job, Status.MOCKUP_SENT, user=manager)
    mockup_version(job)
    with pytest.raises(InvalidMove, match="Pick the mock-up version"):
        services.move(job, Status.MOCKUP_SENT, user=manager, mockup="nope")


# --- Mock-up approved -----------------------------------------------------------------


def test_approval_records_who_which_version_and_every_tick(manager, job_at):
    job = job_at(Status.MOCKUP_SENT)
    version = mockup_version(job)
    job = approve(job, manager, version, approved_by="  Pat   Doe ")
    assert job.status == Status.MOCKUP_APPROVED and job.mockup_approved_at is not None
    approval = MockupApproval.objects.get()
    assert approval.mockup == version and approval.approved_by_name == "Pat Doe"
    assert approval.checks == mockups.DEFAULT_CHECKS and approval.saved_by == manager
    assert job.history.get().note.startswith("Mock-up v1 approved by Pat Doe; Due date set to")
    audit = AuditLog.objects.get(entity_type="jobs.job", action="status_change")
    assert audit.changes["approved_by"] == [None, "Pat Doe"]


def test_every_box_must_be_ticked(manager, job_at):
    job = job_at(Status.MOCKUP_SENT)
    version = mockup_version(job)
    with pytest.raises(InvalidMove, match="Not ticked: Colours checked"):
        approve(
            job, manager, version, ticked=[c for c in mockups.checks() if c != "Colours checked"]
        )
    assert job.history.count() == 0 and not MockupApproval.objects.exists()


def test_who_approved_is_required(manager, job_at):
    job = job_at(Status.MOCKUP_SENT)
    with pytest.raises(InvalidMove, match="Say who approved"):
        approve(job, manager, mockup_version(job), approved_by="   ")


@pytest.mark.parametrize("role", ["production", "avery", "customer_entry"])
def test_only_admin_and_manager_approve(role, make_user, job_at):
    job = job_at(Status.MOCKUP_SENT)
    with pytest.raises(PermissionDenied):
        approve(job, make_user(role), mockup_version(job))


def test_the_boxes_come_from_the_app_setting(manager, job_at):
    AppSetting.objects.create(key=mockups.SETTING, value=["Customer said yes", "Spelling"])
    job = job_at(Status.MOCKUP_SENT)
    job = approve(job, manager, mockup_version(job), ticked=["Customer said yes", "Spelling"])
    assert MockupApproval.objects.get().checks == ["Customer said yes", "Spelling"]


# --- The mock-up gate (FR-12) ----------------------------------------------------------


def test_a_job_cant_pass_mockup_approved_without_approval(manager, job_at):
    with pytest.raises(InvalidMove, match="passes Mock-up approved"):
        services.move(job_at(Status.MOCKUP_SENT), Status.MATERIALS_ORDERED, user=manager, note="x")


def test_an_admin_can_override_the_gate_with_a_reason(make_user, job_at):
    admin = make_user("admin")
    job = services.move(
        job_at(Status.MOCKUP_SENT), Status.MATERIALS_ORDERED, user=admin, note="Repeat order"
    )
    assert job.status == Status.MATERIALS_ORDERED
    audit = AuditLog.objects.get(entity_type="jobs.job", action="override")
    assert audit.changes["mockup_gate"] == [None, "overridden"]


def test_skip_mockup_and_approved_jobs_pass(manager, job_at):
    skipped = services.skip_mockup(job_at(Status.INVOICED), user=manager, note="Reorder")
    assert skipped.status == Status.MATERIALS_ORDERED
    job = job_at(Status.MOCKUP_SENT)
    job = services.move(job, Status.MOCKUP_APPROVED, user=manager, **approval_for(job))
    assert services.move(job, Status.MATERIALS_ORDERED, user=manager).status == (
        Status.MATERIALS_ORDERED
    )


def test_store_orders_arrive_already_past_the_gate(manager, customer):
    job = services.store_order_job(customer=customer, title="Store: hoodie", note="Order 1")
    assert job.status == Status.MOCKUP_APPROVED
    assert services.move(job, Status.MATERIALS_ORDERED, user=manager).status == (
        Status.MATERIALS_ORDERED
    )


# --- Which version is approved -----------------------------------------------------------


def state_of(job):
    return {str(r["version"]): r["state"] for r in mockups.states(job)}


def test_states_follow_the_emails(manager, job_at):
    job = job_at(Status.INVOICED)
    v1 = mockup_version(job)
    assert state_of(job) == {"v1": "draft"}
    job = send(job, manager, v1)
    assert state_of(job) == {"v1": "waiting"}
    # Customer asks for changes: back to Invoiced, v2 drawn and sent.
    job = services.move(job, Status.INVOICED, user=manager, note="Bigger logo")
    v2 = mockup_version(job, "v2.png")
    assert state_of(job) == {"v1": "changes", "v2": "draft"}
    job = send(job, manager, v2)
    assert state_of(job) == {"v1": "superseded", "v2": "waiting"}
    job = approve(job, manager, v2)
    rows = mockups.states(job)
    assert [(str(r["version"]), r["state"]) for r in rows] == [
        ("v2", "approved"),  # first, whatever its number
        ("v1", "superseded"),
    ]
    assert rows[0]["approval"].approved_by_name == "Pat Doe" and mockups.approved(job) == v2


def test_a_new_version_sent_after_approval_replaces_it(manager, job_at):
    job = job_at(Status.MOCKUP_SENT)
    v1 = mockup_version(job)
    job = approve(job, manager, v1)
    job = services.reopen_mockup(job, user=manager, note="Customer wants navy")
    v2 = mockup_version(job, "v2.png")
    assert mockups.approved(job) == v1  # still the one to print until v2 goes out
    job = send(job, manager, v2)
    assert mockups.approved(job) is None and state_of(job)["v2"] == "waiting"


# --- The job page ---------------------------------------------------------------------


def test_the_mockups_tab_makes_the_approved_one_obvious(client, manager, job_at):
    job = job_at(Status.MOCKUP_SENT)
    v1 = mockup_version(job, "first.png")
    v2 = mockup_version(job, "second.png")
    approve(job, manager, v2)
    client.force_login(manager)
    html = client.get(reverse("jobs:detail", args=[job.pk]) + "?tab=mockups").content.decode()
    assert html.index("Mock-up v2") < html.index("Mock-up v1")
    assert 'class="mockup approved"' in html and "APPROVED" in html
    assert 'class="mockup superseded"' in html and "Superseded" in html
    assert "Approved by Pat Doe" in html and "Approved mock-up v2" in html  # header pill
    assert v1.file.name in html


def test_the_approve_form_has_the_boxes_and_check_all(client, manager, job_at):
    job = job_at(Status.MOCKUP_SENT)
    mockup_version(job)
    client.force_login(manager)
    html = client.get(reverse("jobs:detail", args=[job.pk])).content.decode()
    assert "Check all" in html and 'name="approved_by"' in html
    for c in mockups.checks():
        assert f'value="{c}"' in html


def test_approving_through_the_page(client, manager, job_at):
    job = job_at(Status.MOCKUP_SENT)
    version = mockup_version(job)
    client.force_login(manager)
    url = reverse("jobs:action", args=[job.pk])
    form = {"action": "move", "to_status": "mockup_approved", "mockup": version.pk}
    client.post(url, {**form, "approved_by": "Pat Doe", "checks": mockups.checks()[:2]})
    job.refresh_from_db()
    assert job.status == Status.MOCKUP_SENT  # boxes missing
    client.post(url, {**form, "approved_by": "Pat Doe", "checks": mockups.checks()})
    job.refresh_from_db()
    assert job.status == Status.MOCKUP_APPROVED


def test_adding_a_version_through_the_page(client, manager, job_at):
    job = job_at(Status.INVOICED)
    client.force_login(manager)
    client.post(reverse("jobs:mockup_add", args=[job.pk]), {"upload": png(), "note": "First go"})
    assert str(job.mockups.get()) == "v1"


def test_production_sees_versions_but_cant_add_or_approve(client, make_user, job_at):
    job = job_at(Status.MOCKUP_SENT)
    mockup_version(job)
    client.force_login(make_user("production"))
    html = client.get(reverse("jobs:detail", args=[job.pk]) + "?tab=mockups").content.decode()
    assert "Mock-up v1" in html and "Add version" not in html and "Check all" not in html


def test_jumps_dont_offer_the_mockup_stages(make_user, job_at):
    actions = services.actions_for(job_at(Status.INVOICED), make_user("admin"))
    targets = {t["to"] for t in actions["jump_targets"]}
    assert Status.MOCKUP_APPROVED not in targets and Status.MOCKUP_SENT not in targets
    assert actions["forward"]["mockup_form"]
