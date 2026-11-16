"""What Square's API can see that changed recently: for finding out what an estimate leaves
behind, since Square's API has no estimates.

    python manage.py square_probe --live --location L... --minutes 30
    python manage.py square_probe --live --location L... --show <order or invoice id>

Lists orders in every state (drafts included), invoices (drafts included) and customers
updated in the last N minutes. --show prints one order or invoice in full. Square is only
read; nothing is stored in the tracker.
"""

import json
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.integrations import nightly, square

from . import _live

STATES = ["DRAFT", "OPEN", "COMPLETED", "CANCELED"]


class Command(BaseCommand):
    help = "List orders, invoices and customers Square changed recently (read only)."

    def add_arguments(self, parser):
        parser.add_argument("--minutes", type=int, default=30)
        parser.add_argument("--show", help="Print one order or invoice in full.")
        _live.add_arguments(parser)

    def handle(self, *args, minutes, show, live, location, **options):
        _live.apply(live, location)
        if not square.api_configured():
            raise CommandError("Square isn't set up here (SQUARE_ACCESS_TOKEN).")
        try:
            nightly.check_location()
            if show:
                return self._show(show)
            since_dt = timezone.now() - timedelta(minutes=minutes)
            since = since_dt.isoformat()
            self.stdout.write(f"Changed in Square since {timezone.localtime(since_dt):%H:%M}:")
            self._orders(since)
            self._invoices(since_dt)
            self._customers(since)
        except square.SquareError as exc:
            raise CommandError(str(exc)) from exc

    def _locations(self):
        return (
            [settings.SQUARE_LOCATION_ID]
            if settings.SQUARE_LOCATION_ID
            else square.list_locations()
        )

    def _orders(self, since):
        rows = []
        for loc in self._locations():
            rows.extend(square.search_orders(loc, updated_since=since, states=STATES, max_pages=5))
        self.stdout.write(f"\nORDERS ({len(rows)})")
        for o in rows:
            total = square.money(square._cents(o.get("total_money")))
            lines = len(o.get("line_items") or [])
            source = (o.get("source") or {}).get("name") or "-"
            self.stdout.write(
                f"  {o['id']}  {o.get('state')}  source={source}"
                f"  ref={o.get('reference_id') or '-'}  {total}  {lines} line(s)"
                f"  created {o.get('created_at', '')[:16]}  updated {o.get('updated_at', '')[:16]}"
            )

    def _invoices(self, since_dt):
        rows = []
        for loc in self._locations():
            for inv in square.list_invoices(loc, max_pages=20):
                updated = inv.get("updated_at") or ""
                if updated and updated >= since_dt.strftime("%Y-%m-%dT%H:%M"):
                    rows.append(inv)
        self.stdout.write(f"\nINVOICES ({len(rows)})")
        for inv in rows:
            self.stdout.write(
                f"  {inv['id']}  #{inv.get('invoice_number') or '-'}  {inv.get('status')}"
                f"  title={inv.get('title') or '-'}  order={inv.get('order_id') or '-'}"
                f"  updated {inv.get('updated_at', '')[:16]}"
            )

    def _customers(self, since):
        rows = list(square.search_customers(updated_since=since))
        self.stdout.write(f"\nCUSTOMERS ({len(rows)})")
        for c in rows:
            name = " ".join(p for p in (c.get("given_name"), c.get("family_name")) if p)
            self.stdout.write(
                f"  {c['id']}  {name or c.get('company_name') or '-'}"
                f"  updated {c.get('updated_at', '')[:16]}"
            )

    def _show(self, object_id):
        found = square.get_invoice(object_id) if object_id.startswith("inv:") else None
        found = found or square.get_order(object_id)
        if found is None:
            raise CommandError(f"Square has no order or invoice {object_id}.")
        self.stdout.write(json.dumps(found, indent=2, sort_keys=True))
