"""Write audit entries. Callers pass the user who acted, so every entry knows who did it."""

from datetime import date, datetime
from decimal import Decimal

from django.db import models

from .models import AuditLog


def _plain(value):
    """Make a field value safe for JSON."""
    if isinstance(value, models.Model):
        return value.pk
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list | tuple | set):
        return [_plain(v) for v in value]
    return value


def record(actor, entity: models.Model, action: str, changes: dict | None = None, ip=None):
    if actor is not None and not actor.is_authenticated:
        actor = None
    return AuditLog.objects.create(
        actor=actor,
        entity_type=entity._meta.label_lower,
        entity_id=entity.pk,
        action=action,
        changes={k: [_plain(v) for v in pair] for k, pair in changes.items()} if changes else None,
        ip=ip,
    )


def diff(before: dict, after: dict) -> dict:
    """{field: [old, new]} for the fields whose value changed."""
    return {k: [before.get(k), after.get(k)] for k in after if before.get(k) != after.get(k)}
