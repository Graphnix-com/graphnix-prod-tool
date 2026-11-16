"""Usability pass: the board by role, collapsible job tiles, job files, the Invoices link."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.jobs import board, timeline
from apps.jobs.models import Job, JobFile, Status

pytestmark = pytest.mark.django_db


# --- The board by role --------------------------------------------------------------


@pytest.fixture
def jobs(customer):
    return {
        s: Job.objects.create(customer=customer, title=f"Job at {s}", status=s)
        for s in (Status.QUOTING, Status.MOCKUP_SENT, Status.IN_PRODUCTION, Status.QA)
    }


def board_page(client, user, query=""):
    client.force_login(user)
    return client.get(reverse("jobs:list") + query).content.decode()


def test_lanes_by_role(make_user):
    assert board.lanes_for(make_user("admin")) == [
        "Sales",
        "Pre-production",
        "Production",
        "Finish",
    ]
    assert board.lanes_for(make_user("manager")) == [
        "Sales",
        "Pre-production",
        "Production",
        "Finish",
    ]
    assert board.lanes_for(make_user("production")) == ["Production"]
    assert board.lanes_for(make_user("avery")) == ["Production", "Finish"]


def test_production_sees_the_production_lane(client, make_user, jobs):
    page = board_page(client, make_user("production"))
    assert "Job at in_production" in page
    assert "Job at quoting" not in page and "Job at mockup_sent" not in page
    assert "Job at qa" not in page
    assert "Show all stages" in page


def test_avery_also_sees_finish(client, make_user, jobs):
    page = board_page(client, make_user("avery", "Avery"))
    assert "Job at in_production" in page and "Job at qa" in page
    assert "Job at quoting" not in page


def test_show_all_stages(client, make_user, jobs):
    page = board_page(client, make_user("production"), "?all=1")
    assert all(f"Job at {s}" in page for s in jobs)


def test_the_refresh_keeps_the_choice(client, make_user, jobs):
    user = make_user("production")
    assert "Job at quoting" not in board_page(client, user, "?partial=1")
    assert "Job at quoting" in board_page(client, user, "?all=1&partial=1")


def test_the_office_sees_everything_without_a_toggle(client, manager, jobs):
    page = board_page(client, manager)
    assert all(f"Job at {s}" in page for s in jobs)
    assert "Show all stages" not in page


def test_assigned_to_me_still_lists_every_stage(client, make_user, jobs):
    """The production lane filter doesn't hide work assigned to you elsewhere."""
    from apps.jobs.models import JobItem
    from apps.production.models import DecorationMethod, ProductionTask

    rommel = make_user("production", "Rommel")
    job = jobs[Status.MOCKUP_SENT]
    item = JobItem.objects.create(job=job, description="Tees")
    ProductionTask.objects.create(
        job=job,
        job_item=item,
        decoration_method=DecorationMethod.objects.get(code="screen-print"),
        assigned_to=rommel,
    )
    page = board_page(client, rommel)
    mine = page.split('aria-label="Assigned to me"')[1].split("</aside>")[0]
    assert "Job at mockup_sent" in mine


# --- Job tiles ----------------------------------------------------------------------


def test_a_tile_opens_to_show_the_details(client, manager, jobs):
    job = jobs[Status.QUOTING]
    page = board_page(client, manager)
    tile = page.split(f'data-job="{job.pk}"')[1].split("</details>")[0]
    summary = tile.split("<summary>")[1].split("</summary>")[0]
    assert job.title in summary and "Pat Doe" not in summary  # title only, when closed
    assert "Pat Doe" in tile and "Open job" in tile  # all the rest when open
    assert reverse("jobs:detail", args=[job.pk]) in tile


# --- Job files ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


def png(name="mockup.png", size=None):
    data = b"\x89PNG\r\n\x1a\n" + (b"0" * (size or 10))
    return SimpleUploadedFile(name, data, content_type="image/png")


def add(client, job, *uploads):
    return client.post(
        reverse("jobs:file_add", args=[job.pk]), {"files": list(uploads)}, follow=True
    )


def test_production_adds_files_and_they_are_on_the_timeline(client, make_user, job_at):
    job = job_at(Status.IN_PRODUCTION)
    client.force_login(make_user("production", "Rommel"))
    page = add(client, job, png("first print.png"), SimpleUploadedFile("art.pdf", b"%PDF-1.4"))
    assert "Added 2 files" in page.content.decode()
    assert sorted(job.files.values_list("name", flat=True)) == ["art.pdf", "first print.png"]
    titles = [e.title for e in timeline.build(job)]
    assert "File added: first print.png" in titles


@pytest.mark.parametrize("name", ["setup.exe", "notes.docx", "noextension"])
def test_only_images_vectors_and_pdfs(client, manager, job_at, name):
    job = job_at(Status.QUOTING)
    client.force_login(manager)
    page = add(client, job, SimpleUploadedFile(name, b"data")).content.decode()
    assert "only images, vector artwork" in page and not job.files.exists()


def test_vector_formats_are_accepted(client, manager, job_at):
    job = job_at(Status.QUOTING)
    client.force_login(manager)
    add(client, job, *[SimpleUploadedFile(f"logo.{e}", b"data") for e in ("svg", "ai", "eps")])
    assert job.files.count() == 3


def test_a_price_in_the_file_name_is_refused(client, manager, job_at):
    job = job_at(Status.QUOTING)
    client.force_login(manager)
    page = add(client, job, png("quote $450.png")).content.decode()
    assert "Leave prices out" in page and not job.files.exists()


def test_one_bad_file_saves_none(client, manager, job_at):
    job = job_at(Status.QUOTING)
    client.force_login(manager)
    add(client, job, png(), SimpleUploadedFile("virus.exe", b"x"))
    assert not job.files.exists()


def test_too_big_is_refused(client, manager, job_at, monkeypatch):
    from apps.jobs import files

    monkeypatch.setattr(files, "MAX_BYTES", 100)
    job = job_at(Status.QUOTING)
    client.force_login(manager)
    page = add(client, job, png(size=200)).content.decode()
    assert "is over" in page and not job.files.exists()


def test_downloads_photos_inline_and_svg_as_a_download(client, manager, make_user, job_at):
    job = job_at(Status.QUOTING)
    client.force_login(manager)
    add(client, job, png(), SimpleUploadedFile("logo.svg", b"<svg></svg>"))
    photo, svg = job.files.get(name="mockup.png"), job.files.get(name="logo.svg")
    client.force_login(make_user("production"))
    response = client.get(reverse("jobs:file_download", args=[job.pk, photo.pk]))
    assert response.status_code == 200 and b"".join(response.streaming_content).startswith(
        b"\x89PNG"
    )
    assert response["Content-Disposition"].startswith("inline")
    assert "sandbox" in response["Content-Security-Policy"]
    response = client.get(reverse("jobs:file_download", args=[job.pk, svg.pk]))
    assert response["Content-Disposition"].startswith("attachment")


def test_a_file_only_downloads_under_its_own_job(client, manager, job_at):
    job, other = job_at(Status.QUOTING), job_at(Status.QUOTING)
    client.force_login(manager)
    add(client, job, png())
    f = job.files.get()
    assert client.get(reverse("jobs:file_download", args=[other.pk, f.pk])).status_code == 404


def test_signed_out_people_cant_download(client, manager, job_at):
    job = job_at(Status.QUOTING)
    client.force_login(manager)
    add(client, job, png())
    f = job.files.get()
    client.logout()
    assert client.get(reverse("jobs:file_download", args=[job.pk, f.pk])).status_code == 302


def test_only_the_office_removes_files(
    client, manager, make_user, job_at, media, django_capture_on_commit_callbacks
):
    job = job_at(Status.QUOTING)
    client.force_login(manager)
    add(client, job, png())
    f = job.files.get()
    stored = media / f.file.name
    assert stored.exists()

    client.force_login(make_user("production"))
    page = client.get(reverse("jobs:detail", args=[job.pk]) + "?tab=details").content.decode()
    assert "Remove" not in page and "mockup.png" in page
    client.post(reverse("jobs:file_delete", args=[job.pk, f.pk]))
    assert JobFile.objects.filter(pk=f.pk).exists()

    client.force_login(manager)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("jobs:file_delete", args=[job.pk, f.pk]))
    assert not JobFile.objects.filter(pk=f.pk).exists() and not stored.exists()
    assert AuditLog.objects.filter(
        entity_type="jobs.jobfile", action="delete", actor=manager
    ).exists()


# --- The Invoices link --------------------------------------------------------------


def test_the_header_says_invoices_for_the_office_only(client, manager, make_user):
    client.force_login(manager)
    page = client.get(reverse("jobs:list")).content.decode()
    assert f'href="{reverse("integrations:inbox")}"' in page and ">Invoices<" in page
    client.force_login(make_user("production"))
    page = client.get(reverse("jobs:list")).content.decode()
    assert reverse("integrations:inbox") not in page


# --- "Requests", not "Inquiries" ----------------------------------------------------


def test_requests_everywhere_people_look(client, manager):
    client.force_login(manager)
    for url in (reverse("home"), reverse("sales:list"), reverse("sales:new")):
        page = client.get(url).content.decode()
        visible = page.split("<body>")[1]
        assert "Requests" in visible or "request" in visible
        for word in ("Inquiry", "Inquiries", " inquiry", " inquiries", "inquir("):
            assert word not in visible.replace("_inquiry", "").replace("inquiry.", ""), (url, word)


def test_old_inquiry_links_still_work(client, manager):
    client.force_login(manager)
    response = client.get("/inquiries/new/")
    assert response.status_code == 301 and response["Location"] == reverse("sales:new")


def test_an_open_tile_starts_with_no_blank_line(client, manager, jobs):
    page = board_page(client, manager)
    more = page.split('<div class="more">')[1].split("</div>", 1)[0]
    assert not more.strip().startswith("<br")
