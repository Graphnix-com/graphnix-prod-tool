"""Pickup or shipment (FR-27) and closing shipped jobs.

Ready → Shipped / picked up only through this form (Avery and the Managers). A shipped job
closes by itself `close_after_days` days later (2 unless the app setting says otherwise;
decided 17 Nov 2026).
"""

from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from . import flow, services
from .models import Job, Shipment, Status

CLOSE_SETTING = "close_after_days"
CLOSE_DEFAULT = 2


def default_address(job: Job) -> str:
    """The customer's shipping address (else billing), as lines of text."""
    address = (
        job.customer.addresses.filter(kind="shipping").first() or job.customer.addresses.first()
    )
    if address is None:
        return ""
    parts = [
        address.line1,
        address.line2,
        f"{address.city} {address.province} {address.postal_code}",
    ]
    return "\n".join(p for p in parts if p and p.strip())


@transaction.atomic
def record_shipment(job: Job, *, user, data: dict) -> Shipment:
    if not user.has_perm(flow.FINISH):
        raise PermissionDenied("You can't record pickups or shipments.")
    if job.status != Status.READY:
        raise ValidationError(f"{job.display_number} isn't Ready.")
    method = data.get("method", "")
    if method not in Shipment.Method.values:
        raise ValidationError("Pick up, courier or delivery?")
    shipment = Shipment(
        job=job,
        method=method,
        ship_date=data.get("ship_date") or timezone.localdate(),
        notes=(data.get("notes") or "").strip(),
        created_by=user,
    )
    if method == Shipment.Method.PICKUP:
        shipment.picked_up_by = " ".join((data.get("picked_up_by") or "").split())[:200]
        if not shipment.picked_up_by:
            raise ValidationError("Who collected it?")
    else:
        shipment.address = (data.get("address") or "").strip()
    if method == Shipment.Method.COURIER:
        shipment.carrier = data.get("carrier", "")
        if shipment.carrier not in Shipment.Carrier.values:
            raise ValidationError("Which courier?")
        shipment.carrier_other = (data.get("carrier_other") or "").strip()[:100]
        if shipment.carrier == Shipment.Carrier.OTHER and not shipment.carrier_other:
            raise ValidationError("Name the courier.")
        shipment.tracking_number = (data.get("tracking_number") or "").strip()[:100]
    shipment.save()
    services.move(job, Status.SHIPPED, user=user, note=shipment.summary, shipment_recorded=True)
    return shipment


def close_after_days() -> int:
    from apps.accounts.models import get_setting

    value = get_setting(CLOSE_SETTING)
    return value if isinstance(value, int) and value >= 0 else CLOSE_DEFAULT


def close_shipped(now=None) -> list[Job]:
    """Close every job shipped or picked up at least `close_after_days` days ago."""
    now = now or timezone.now()
    days = close_after_days()
    due = Job.objects.filter(
        status=Status.SHIPPED, status_changed_at__lte=now - timedelta(days=days)
    )
    closed = []
    for job in due:
        if not services.system_move(job, Status.CLOSED, note=f"Closed {days} days after shipping"):
            closed.append(job)
    return closed
