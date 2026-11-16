"""The nightly check against Square (INT-01), the go-live customer import (FR-08), and a
hand-picked invoice import for trying real invoices on staging.

Webhooks can be missed: Square gives up after its retry window, or the server was down.
Each night the worker:
1. checks the token can read the configured location (and says so on the health panel);
2. re-reads every invoice linked to an open job;
3. reads invoices updated in the last 3 days, to catch any never seen;
3b. reads store orders (Square Online) updated in the last 3 days, the same way;
4. retries stored events that never finished;
5. deletes webhook bodies older than 90 days (they hold customer names and emails).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.accounts.models import AppSetting, get_setting
from apps.jobs.models import Status

from . import square
from .models import SquareInvoice, SquareOrder, WebhookEvent

log = logging.getLogger(__name__)

RESULT_KEY = "square_nightly"
KEEP_EVENTS_DAYS = 90
RECENT_DAYS = 3


def last_result() -> dict | None:
    return get_setting(RESULT_KEY)


def _save(result: dict) -> dict:
    AppSetting.objects.update_or_create(key=RESULT_KEY, defaults={"value": result})
    return result


def _refresh(data: dict) -> bool:
    """Apply Square's copy of an invoice; True if it changed what we had."""
    stored = SquareInvoice.objects.filter(square_id=data["id"]).first()
    refunded = None
    if data.get("status") in square.REFUND_STATES and data.get("order_id"):
        refunded = square.refunded_cents(square.get_order(data["order_id"]))
    before = (stored.version, stored.refunded_cents) if stored else None
    after = square.apply(data, refunded=refunded, event_id="nightly")
    if after is None:
        return False
    return before is None or (after.version, after.refunded_cents) != before


def run(now=None) -> dict:
    now = now or timezone.now()
    result = {"at": now.isoformat(), "checked": 0, "fixed": 0, "retried": 0, "error": ""}
    deleted, _ = WebhookEvent.objects.filter(
        received_at__lt=now - timedelta(days=KEEP_EVENTS_DAYS)
    ).delete()
    result["deleted_events"] = deleted
    if not square.api_configured():
        result["error"] = "Square isn't set up on this server (SQUARE_ACCESS_TOKEN)."
        return _save(result)
    try:
        location = settings.SQUARE_LOCATION_ID
        locations = square.list_locations()
        if location and location not in locations:
            result["error"] = (
                f"The access token can't read location {location}. It belongs to a Square "
                f"account with location(s) {', '.join(locations) or 'none'}. Check "
                "SQUARE_ACCESS_TOKEN and SQUARE_LOCATION_ID are from the same account."
            )
            return _save(result)

        seen = set()
        open_linked = SquareInvoice.objects.filter(job__isnull=False).exclude(
            job__status__in=[Status.CLOSED, Status.CANCELLED]
        )
        for invoice in open_linked:
            data = square.get_invoice(invoice.square_id)
            seen.add(invoice.square_id)
            result["checked"] += 1
            if data is not None and _refresh(data):
                result["fixed"] += 1

        since = now - timedelta(days=RECENT_DAYS)
        for loc in [location] if location else locations:
            for data in square.list_invoices(loc):
                updated = parse_datetime(data.get("updated_at") or "")
                if data["id"] in seen or (updated and updated < since):
                    continue
                seen.add(data["id"])
                result["checked"] += 1
                if _refresh(data):
                    result["fixed"] += 1

        # Store orders (Square Online) updated in the same window: any whose webhook was
        # missed becomes its job now.
        from . import orders

        result["orders"] = 0
        updated_since = since.isoformat()
        for loc in [location] if location else locations:
            for order in square.search_orders(loc, updated_since=updated_since):
                if not orders.is_store_order(order):
                    continue
                result["orders"] += 1
                stored = SquareOrder.objects.filter(square_id=order["id"]).first()
                before = (stored.version, stored.job_id) if stored else None
                after = orders.apply(
                    order, square_customer=orders.buyer_record(order), event_id="nightly"
                )
                if after is not None and (after.version, after.job_id) != before:
                    result["fixed"] += 1

        for event in WebhookEvent.objects.filter(processed_at__isnull=True):
            try:
                square.process_event(event.pk)
                result["retried"] += 1
            except Exception:  # still failing: stays on the health panel
                log.warning("Square event %s still failing in the nightly check", event.event_id)
    except square.SquareError as exc:
        result["error"] = str(exc)
    return _save(result)


def import_customers(*, years: int = 2, dry_run: bool = False, progress=None) -> dict:
    """Tracker customers for the Square customers invoiced in the last `years` years
    (question 8). A Square customer already linked is left alone; one whose email matches
    a tracker customer is linked to it; the rest are created. Square is never written.

    `progress(text)`, if given, is called as it goes: it reads every invoice first, then
    each customer one at a time, which takes a few minutes for a thousand."""
    from django.db import transaction

    from apps.audit.services import record
    from apps.customers.models import Customer

    since = timezone.now() - timedelta(days=365 * years)
    say = progress or (lambda text: None)
    ids: dict[str, None] = {}  # in order, without repeats
    invoices = 0
    locations = (
        [settings.SQUARE_LOCATION_ID] if settings.SQUARE_LOCATION_ID else square.list_locations()
    )
    for loc in locations:
        for data in square.list_invoices(loc, max_pages=200):
            invoices += 1
            if invoices % 500 == 0:
                say(f"Read {invoices} invoices…")
            created = parse_datetime(data.get("created_at") or "")
            cid = (data.get("primary_recipient") or {}).get("customer_id")
            if cid and (created is None or created >= since):
                ids.setdefault(cid)
    say(f"Read {invoices} invoices: {len(ids)} customers to check.")

    counts = {"square_customers": len(ids), "already_linked": 0, "matched": 0, "created": 0}
    for done, cid in enumerate(ids, 1):
        if done % 100 == 0:
            say(f"Checked {done} of {len(ids)} customers…")
        if Customer.objects.filter(square_customer_id=cid).exists():
            counts["already_linked"] += 1
            continue
        sq = square.get_customer(cid)
        if not sq:
            continue
        email = (sq.get("email_address") or "").strip()
        match = (
            list(Customer.objects.filter(email__iexact=email, square_customer_id="")[:2])
            if email
            else []
        )
        if len(match) == 1:
            counts["matched"] += 1
            if not dry_run:
                square.match_customer(match[0], cid, how="email")
            continue
        counts["created"] += 1
        if dry_run:
            continue
        name = " ".join(p for p in (sq.get("given_name"), sq.get("family_name")) if p)
        company = sq.get("company_name") or ""
        with transaction.atomic():
            customer = Customer.objects.create(
                display_name=(name or company or email or cid)[:200],
                company_name=company[:200],
                email=email,
                phone=(sq.get("phone_number") or "")[:40],
                square_customer_id=cid,
            )
            record(None, customer, "create", {"source": [None, "Square import"]})
    return counts


def check_location() -> None:
    """Raise SquareAccessError if the token can't read SQUARE_LOCATION_ID."""
    location = settings.SQUARE_LOCATION_ID
    locations = square.list_locations()
    if location and location not in locations:
        raise square.SquareAccessError(
            f"The access token can't read location {location}. It belongs to a Square "
            f"account with location(s) {', '.join(locations) or 'none'}. Check "
            "SQUARE_ACCESS_TOKEN and SQUARE_LOCATION_ID are from the same account."
        )


def pick_invoices(*, ids=(), numbers=(), latest: int = 0) -> list[dict]:
    """Square invoices by ID, by invoice number, or the `latest` most recently created."""
    found = {}
    for invoice_id in ids:
        data = square.get_invoice(invoice_id)
        if data is None:
            raise square.SquareError(f"Square has no invoice {invoice_id}.")
        found[data["id"]] = data
    if numbers or latest:
        wanted = {str(n).strip() for n in numbers}
        listed = []
        location = settings.SQUARE_LOCATION_ID
        for loc in [location] if location else square.list_locations():
            listed.extend(square.list_invoices(loc, max_pages=200))
        for data in listed:
            if data.get("invoice_number") in wanted:
                found[data["id"]] = data
        missing = wanted - {d.get("invoice_number") for d in found.values()}
        if missing:
            raise square.SquareError(
                f"Square has no invoice numbered {', '.join(sorted(missing))}."
            )
        listed.sort(key=lambda d: d.get("created_at") or "", reverse=True)
        for data in listed[:latest]:
            found[data["id"]] = data
    return list(found.values())


def import_invoices(invoices: list[dict], *, dry_run: bool = False) -> list[dict]:
    """Store each invoice as a webhook would. One with a job's G- number links itself; the
    rest wait under Invoices. Square is never written."""
    rows = []
    for data in invoices:
        fields = square.read_invoice(data)
        row = {
            "number": fields["invoice_number"] or data["id"],
            "title": fields["title"],
            "status": fields["status"],
            "total": square.money(fields["total_cents"], fields["currency"]),
        }
        if dry_run:
            row["result"] = "would import"
        elif square.other_location(data):
            row["result"] = "skipped: another location"
        else:
            _refresh(data)
            stored = SquareInvoice.objects.get(square_id=data["id"])
            row["result"] = (
                f"linked to {stored.job.display_number}"
                if stored.job_id
                else "waiting under Invoices"
            )
        rows.append(row)
    return rows
