from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import CommandError, call_command
from django.urls import reverse
from django.utils import timezone

from apps.core import health
from apps.core.models import Heartbeat

pytestmark = pytest.mark.django_db
User = get_user_model()


# --- /healthz (Docker health check) ---------------------------------------------


def test_healthz_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_reports_database_down(client, monkeypatch):
    monkeypatch.setattr(health, "database_ok", lambda: False)
    assert client.get("/healthz").status_code == 503


# --- Worker heartbeat ---------------------------------------------------------


def test_worker_check_missing_fresh_and_stale():
    assert health.check_worker().status == health.FAIL
    now = timezone.now()
    Heartbeat.objects.create(name="worker", last_seen=now - timedelta(seconds=30))
    assert health.check_worker(now).status == health.OK
    assert health.check_worker(now + timedelta(minutes=10)).status == health.FAIL


def test_heartbeat_task_records_worker():
    from apps.core.tasks import worker_heartbeat

    worker_heartbeat(timestamp=0)
    assert Heartbeat.objects.filter(name="worker").exists()


def test_check_heartbeat_command():
    with pytest.raises(CommandError):
        call_command("check_heartbeat", "worker")
    Heartbeat.objects.create(name="worker", last_seen=timezone.now())
    call_command("check_heartbeat", "worker", "--max-age", "60")
    Heartbeat.objects.filter(name="worker").update(last_seen=timezone.now() - timedelta(minutes=5))
    with pytest.raises(CommandError):
        call_command("check_heartbeat", "worker", "--max-age", "60")


# --- Backup stamps --------------------------------------------------------------


def test_backup_checks_read_stamp_files(settings, tmp_path):
    settings.BACKUP_STATE_DIR = tmp_path
    settings.APP_ENV = "production"
    assert health.check_backup_created().status == health.WARN
    assert health.check_backup_pulled().status == health.WARN

    (tmp_path / "last_backup_production").write_text("2026-11-03T09:30:00Z\n")
    (tmp_path / "last_pull").write_text("2026-11-01T09:30:00Z\n")
    now = timezone.datetime(2026, 11, 3, 15, 0, tzinfo=timezone.get_fixed_timezone(0))
    assert health.check_backup_created(now).status == health.OK
    assert health.check_backup_pulled(now).status == health.FAIL


def test_garbled_stamp_counts_as_missing(settings, tmp_path):
    settings.BACKUP_STATE_DIR = tmp_path
    (tmp_path / "last_pull").write_text("not a date")
    assert health.check_backup_pulled().status == health.WARN


# --- Health panel ---------------------------------------------------------------


def test_panel_requires_sign_in(client):
    response = client.get(reverse("core:health"))
    assert response.status_code == 302
    assert "/admin/login/" in response["Location"]


def test_panel_forbidden_without_permission(client):
    user = User.objects.create_user("floor@graphnix.com", "pw-123456789", full_name="Floor")
    client.force_login(user)
    assert client.get(reverse("core:health")).status_code == 403


def test_panel_shows_checks_with_permission(client):
    user = User.objects.create_user("mgr@graphnix.com", "pw-123456789", full_name="Manager")
    user.user_permissions.add(Permission.objects.get(codename="view_system_health"))
    client.force_login(user)
    response = client.get(reverse("core:health"))
    assert response.status_code == 200
    body = response.content.decode()
    for name in ("Database", "Background worker", "Nightly backup", "shop&#x27;s backup server"):
        assert name in body
