"""Online store orders (Square Online) into the pipeline.

A store order is paid at checkout and its design is the store's, already approved, so each
one becomes a job at Mock-Up Approved: the buyer as the customer, the order's lines as job
items (sizes read from the variation names), payment from the order. Counter sales are
Square orders too: only orders whose source is in SQUARE_STORE_SOURCES are stored.

Square is only read. Network calls happen in `fetch`, outside any lock; `apply` writes.
"""

from __future__ import annotations

import logging
import math
import re
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils.dateparse import parse_datetime

from apps.audit.services import record

from . import square
from .models import SquareOrder

log = logging.getLogger(__name__)

# Orders that are real sales; a draft isn't placed yet.
PLACED = {"OPEN", "COMPLETED"}
# Line items that are made: not gift cards.
MADE = {"ITEM", "CUSTOM_AMOUNT", ""}


# --- Which orders ------------------------------------------------------------------


def source_name(order: dict) -> str:
    return str((order.get("source") or {}).get("name") or "")


def is_store_order(order: dict) -> bool:
    name = source_name(order).casefold()
    return bool(name) and any(s.strip().casefold() in name for s in settings.SQUARE_STORE_SOURCES)


# --- Reading an order --------------------------------------------------------------


def _recipient(order: dict) -> dict:
    for f in order.get("fulfillments") or []:
        for key in ("shipment_details", "pickup_details", "delivery_details"):
            r = (f.get(key) or {}).get("recipient")
            if r:
                return r
    return {}


def read_order(order: dict) -> dict:
    total = square._cents(order.get("total_money"))
    due = square._cents(order.get("net_amount_due_money"))
    if total is not None and due is not None:
        paid = max(total - due, 0)
    else:
        paid = sum(square._cents(t.get("amount_money")) or 0 for t in order.get("tenders") or [])
    fulfillments = order.get("fulfillments") or [{}]
    return {
        "version": int(order.get("version") or 0),
        "location_id": order.get("location_id") or "",
        "source_name": source_name(order)[:255],
        "reference": str(order.get("reference_id") or order.get("ticket_name") or "")[:100],
        "state": str(order.get("state") or ""),
        "square_customer_id": order.get("customer_id") or "",
        "buyer_name": str(_recipient(order).get("display_name") or "")[:255],
        "fulfillment": str(fulfillments[0].get("type") or "")[:20],
        "currency": square._currency(order.get("total_money")) or "CAD",
        "total_cents": total,
        "paid_cents": paid,
        "refunded_cents": square.refunded_cents(order) or 0,
        "square_created_at": parse_datetime(order.get("created_at") or ""),
        "square_updated_at": parse_datetime(order.get("updated_at") or ""),
    }


# --- Sizes from variation names -----------------------------------------------------

_SIZES = {
    "xs": "XS", "xsmall": "XS", "x-small": "XS", "extra small": "XS",
    "s": "S", "sm": "S", "small": "S",
    "m": "M", "med": "M", "medium": "M",
    "l": "L", "lg": "L", "large": "L",
    "xl": "XL", "xlarge": "XL", "x-large": "XL", "extra large": "XL",
    "2xl": "2XL", "xxl": "2XL", "2x": "2XL", "xx-large": "2XL", "2x-large": "2XL",
    "3xl": "3XL", "xxxl": "3XL", "3x": "3XL", "xxx-large": "3XL", "3x-large": "3XL",
    "4xl": "4XL", "4x": "4XL", "5xl": "5XL", "5x": "5XL",
}  # fmt: skip
_YOUTH = {"XS": "YXS", "S": "YS", "M": "YM", "L": "YL", "XL": "YXL"}
_YOUTH_PREFIX = re.compile(r"^(youth|yth|y)[\s-]+")


def size_of(text: str) -> str | None:
    """'Large' → 'L', 'Youth Medium' → 'YM', 'YS' → 'YS'; None if it isn't a size."""
    t = " ".join(text.casefold().replace("_", " ").split())
    if t.upper() in _YOUTH.values():
        return t.upper()
    youth = _YOUTH_PREFIX.match(t)
    if youth:
        base = _SIZES.get(t[youth.end() :])
        return _YOUTH.get(base) if base else None
    return _SIZES.get(t)


def split_variation(name: str) -> tuple[str | None, str]:
    """('L', 'Black') from 'Black / L'. Square's default variation name 'Regular' is none."""
    parts = [p.strip() for p in re.split(r"\s*[/,|]\s*|\s+-\s+", name or "") if p.strip()]
    size = None
    rest = []
    for part in parts:
        if size is None and size_of(part):
            size = size_of(part)
        elif part.casefold() != "regular":
            rest.append(part)
    return size, ", ".join(rest)


def _quantity(text) -> int:
    try:
        return max(1, math.ceil(Decimal(str(text or "1"))))
    except InvalidOperation:
        return 1


def items_from(order: dict) -> list[dict]:
    """Job items: one per product and colour, its sizes added up; personalization and
    notes from each line kept in the item's notes."""
    grouped: dict[tuple, dict] = {}
    for line in order.get("line_items") or []:
        if str(line.get("item_type") or "") not in MADE:
            continue
        size, colour = split_variation(line.get("variation_name") or "")
        name = str(line.get("name") or "Item")[:200]
        item = grouped.setdefault(
            (name, colour), {"description": name, "color": colour[:60], "sizes": {}, "notes": []}
        )
        qty = _quantity(line.get("quantity"))
        key = size or "OS"
        item["sizes"][key] = item["sizes"].get(key, 0) + qty
        extras = [m.get("name") for m in line.get("modifiers") or [] if m.get("name")]
        if line.get("note"):
            extras.append(line["note"])
        if extras:
            item["notes"].append(f"{size or 'Each'} ×{qty}: {'; '.join(extras)}")
    for item in grouped.values():
        item["is_sized"] = set(item["sizes"]) != {"OS"}
        if item["is_sized"] and "OS" in item["sizes"]:
            # Some lines had no size: keep them, but say so.
            item["notes"].append(f"No size given for {item['sizes']['OS']}.")
    return list(grouped.values())


# --- Fetching (network, outside locks) ----------------------------------------------


def fetch(order_id: str) -> tuple[dict | None, dict | None]:
    """The order as Square has it now, and the buyer's Square customer record when the
    order is a store order that still needs a tracker customer."""
    order = square.get_order(order_id)
    if order is None or not is_store_order(order):
        return order, None
    return order, buyer_record(order)


def buyer_record(order: dict) -> dict | None:
    from apps.customers.models import Customer

    cid = order.get("customer_id") or ""
    if not cid or Customer.objects.filter(square_customer_id=cid).exists():
        return None
    if SquareOrder.objects.filter(square_id=order["id"], job__isnull=False).exists():
        return None
    return square.get_customer(cid)


# --- Applying ----------------------------------------------------------------------


def _customer(order: dict, square_customer: dict | None):
    """The buyer as a tracker customer: linked by Square ID, else matched by email (and
    linked), else created."""
    from apps.customers.models import Customer

    cid = order.get("customer_id") or ""
    if cid:
        found = Customer.objects.filter(square_customer_id=cid).first()
        if found:
            return found
    sq = square_customer or {}
    r = _recipient(order)
    email = (sq.get("email_address") or r.get("email_address") or "").strip()
    if email:
        match = list(Customer.objects.filter(email__iexact=email)[:2])
        if len(match) == 1:
            square.match_customer(match[0], cid, how="store order")
            return match[0]
    name = " ".join(p for p in (sq.get("given_name"), sq.get("family_name")) if p)
    name = name or r.get("display_name") or sq.get("company_name") or email
    customer = Customer.objects.create(
        display_name=(name or f"Store order {order['id'][-6:]}")[:200],
        company_name=(sq.get("company_name") or "")[:200],
        email=email,
        phone=(sq.get("phone_number") or r.get("phone_number") or "")[:40],
        square_customer_id=cid
        if cid and not Customer.objects.filter(square_customer_id=cid).exists()
        else "",
    )
    record(None, customer, "create", {"source": [None, "Online store order"]})
    return customer


def title_for(items: list[dict], limit: int = 90) -> str:
    """ "Store: <first product>", "+ N more" for other products (not other colours); long
    product names cut at a word."""
    names = list(dict.fromkeys(i["description"] for i in items)) or ["items"]
    first = names[0]
    if len(first) > limit:
        first = first[:limit].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
    more = f" + {len(names) - 1} more" if len(names) > 1 else ""
    return f"Store: {first}{more}"


# Square's fulfilment types as the job's fulfilment method.
_FULFILMENT = {"SHIPMENT": "ship", "PICKUP": "pickup", "DELIVERY": "deliver"}


def _make_job(stored: SquareOrder, order: dict, square_customer, event_id: str):
    from apps.jobs import services as job_services
    from apps.jobs.models import JobItem, JobItemSize

    items = items_from(order)
    job = job_services.store_order_job(
        customer=_customer(order, square_customer),
        title=title_for(items),
        note=f"Online store order {stored.number} ({stored.source_name})",
        fulfillment=_FULFILMENT.get(stored.fulfillment, ""),
        event_id=event_id,
        number=stored.reference,
    )
    for item in items:
        row = JobItem.objects.create(
            job=job,
            description=item["description"],
            color=item["color"],
            is_sized=item["is_sized"],
            notes="\n".join(item["notes"]),
        )
        for size, qty in item["sizes"].items():
            JobItemSize.objects.create(job_item=row, size=size, quantity=qty)
    return job


@transaction.atomic
def apply(
    order: dict, *, square_customer: dict | None = None, event_id: str = ""
) -> SquareOrder | None:
    """Store a store order's latest copy; make its job the first time it's placed; bring the
    job's payment up to date. Not a store order, or another location: nothing (None)."""
    if not is_store_order(order) or square.other_location(order):
        return None
    stored = SquareOrder.objects.select_for_update().filter(square_id=order["id"]).first()
    fields = read_order(order)
    if stored is not None and fields["version"] < stored.version:
        return stored
    stored = stored or SquareOrder(square_id=order["id"])
    for name, value in fields.items():
        setattr(stored, name, value)
    stored.save()
    if stored.job_id is None and stored.state in PLACED:
        stored.job = _make_job(stored, order, square_customer, event_id)
        stored.save(update_fields=["job", "updated_at"])
    if stored.job_id:
        square.refresh_job(stored.job, order=stored, event_id=event_id)
    return stored


def import_orders(orders: list[dict], *, dry_run: bool = False) -> list[dict]:
    """For the import command: store orders through the same path as a webhook."""
    rows = []
    for order in orders:
        fields = read_order(order)
        row = {
            "number": fields["reference"] or order["id"][-6:],
            "source": fields["source_name"] or "(no source)",
            "state": fields["state"],
            "total": square.money(fields["total_cents"], fields["currency"]),
            "store": is_store_order(order),
        }
        if not row["store"]:
            row["result"] = "not a store order"
        elif dry_run:
            row["result"] = "would import"
        else:
            stored = apply(order, square_customer=buyer_record(order), event_id="import")
            if stored is None:
                row["result"] = "skipped: another location"
            elif stored.job_id:
                row["result"] = f"job {stored.job.display_number}"
            else:
                row["result"] = f"stored ({stored.state.lower()}), no job"
        rows.append(row)
    return rows
