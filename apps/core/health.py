"""Checks shown on the health panel (NFR-08).

Each check returns a HealthCheck. Later build steps add their own here:
Step 2 added Square; Step 4 adds failed emails.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .models import Heartbeat

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass(frozen=True)
class HealthCheck:
    name: str
    status: str  # ok | warn | fail
    detail: str
    when: datetime | None = None


def database_ok() -> bool:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            return cursor.fetchone() == (1,)
    except Exception:
        return False


def check_database() -> HealthCheck:
    if database_ok():
        return HealthCheck("Database", OK, "Connected")
    return HealthCheck("Database", FAIL, "Cannot reach PostgreSQL")


def check_worker(now: datetime | None = None) -> HealthCheck:
    now = now or timezone.now()
    beat = Heartbeat.objects.filter(name="worker").first()
    if beat is None:
        return HealthCheck("Background worker", FAIL, "Has never reported in")
    age = (now - beat.last_seen).total_seconds()
    if age <= settings.WORKER_HEARTBEAT_MAX_AGE:
        return HealthCheck("Background worker", OK, f"Last seen {int(age)} s ago", beat.last_seen)
    return HealthCheck(
        "Background worker", FAIL, f"Silent for {int(age // 60)} min", beat.last_seen
    )


def _read_stamp(path: Path) -> datetime | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _check_stamp(name: str, filename: str, missing: str, now: datetime | None) -> HealthCheck:
    now = now or timezone.now()
    stamp = _read_stamp(Path(settings.BACKUP_STATE_DIR) / filename)
    if stamp is None:
        return HealthCheck(name, WARN, missing)
    if now - stamp <= timedelta(hours=settings.BACKUP_MAX_AGE_HOURS):
        return HealthCheck(name, OK, "Within the last day", stamp)
    days = (now - stamp).days
    return HealthCheck(name, FAIL, f"Last one {days} day(s) ago", stamp)


def check_backup_created(now: datetime | None = None) -> HealthCheck:
    return _check_stamp(
        "Nightly backup made on the server",
        f"last_backup_{settings.APP_ENV}",
        "No backup recorded yet",
        now,
    )


def check_backup_pulled(now: datetime | None = None) -> HealthCheck:
    return _check_stamp(
        "Backup copied to the shop's backup server",
        "last_pull",
        "The shop's backup server has not pulled a copy yet",
        now,
    )


def check_square_webhooks(now: datetime | None = None) -> HealthCheck:
    """Step 2 (INT-01, NFR-12): deliveries arriving and being processed."""
    from apps.integrations.models import WebhookEvent

    now = now or timezone.now()
    name = "Square webhooks"
    last = WebhookEvent.objects.order_by("-received_at").first()
    stuck = WebhookEvent.objects.filter(
        processed_at__isnull=True, received_at__lt=now - timedelta(minutes=10)
    )
    count = stuck.count()
    if count:
        first = stuck.exclude(last_error="").order_by("-received_at").first()
        why = f": {first.last_error[:200]}" if first else ""
        return HealthCheck(name, FAIL, f"{count} event(s) not processed{why}", last.received_at)
    skipped = (
        WebhookEvent.objects.filter(received_at__gte=now - timedelta(days=7))
        .exclude(last_error="")
        .order_by("-received_at")
        .first()
    )
    if skipped:
        return HealthCheck(name, WARN, skipped.last_error[:250], skipped.received_at)
    if last is None:
        return HealthCheck(name, WARN, "Nothing received from Square yet")
    return HealthCheck(name, OK, f"Last: {last.event_type}", last.received_at)


def check_square_nightly(now: datetime | None = None) -> HealthCheck:
    from apps.integrations.nightly import last_result

    now = now or timezone.now()
    name = "Square nightly check"
    result = last_result()
    if not result:
        return HealthCheck(name, WARN, "Hasn't run yet (runs at 03:00)")
    at = datetime.fromisoformat(result["at"])
    if result.get("error"):
        return HealthCheck(name, FAIL, result["error"], at)
    detail = f"Checked {result.get('checked', 0)} invoice(s), fixed {result.get('fixed', 0)}"
    if now - at > timedelta(hours=26):
        return HealthCheck(name, WARN, f"Last ran {(now - at).days + 1} day(s) ago. {detail}", at)
    return HealthCheck(name, OK, detail, at)


def all_checks() -> list[HealthCheck]:
    checks = [check_database()]
    if checks[0].status == OK:
        checks.append(check_worker())
        if settings.SQUARE_ENABLED:
            checks += [check_square_webhooks(), check_square_nightly()]
    checks += [check_backup_created(), check_backup_pulled()]
    return checks
