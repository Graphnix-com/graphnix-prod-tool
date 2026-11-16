"""Online store orders from Square into the tracker, as if their webhooks had arrived.

    python manage.py import_square_orders --list                  # recent orders by source
    python manage.py import_square_orders --latest 3 --dry-run
    python manage.py import_square_orders --latest 3
    python manage.py import_square_orders --id <order id>
    ... --live --location L...   # the real account, token typed at a hidden prompt

Only orders whose source is in SQUARE_STORE_SOURCES (default "Square Online") become jobs;
--list shows every recent order's source, to check the setting. Safe to run again. Square
is only read.
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.integrations import nightly, orders, square

from . import _live


class Command(BaseCommand):
    help = "List recent Square orders, or import online store orders as jobs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", dest="show", help="Recent orders and their source."
        )
        parser.add_argument("--latest", type=int, default=0, help="The N newest store orders.")
        parser.add_argument("--id", action="append", default=[], dest="ids")
        parser.add_argument("--days", type=int, default=30, help="How far back to look.")
        parser.add_argument("--dry-run", action="store_true")
        _live.add_arguments(parser)

    def handle(self, *args, show, latest, ids, days, dry_run, live, location, **options):
        _live.apply(live, location)
        if not square.api_configured():
            raise CommandError("Square isn't set up here (SQUARE_ACCESS_TOKEN).")
        if not (show or latest or ids):
            raise CommandError("Say what: --list, --latest N or --id.")
        if latest > 20:
            raise CommandError("--latest is for a few samples: 20 at most.")
        try:
            nightly.check_location()
            if show:
                return self._list(days)
            picked = []
            for order_id in ids:
                order = square.get_order(order_id)
                if order is None:
                    raise CommandError(f"Square has no order {order_id}.")
                picked.append(order)
            if latest:
                store = [o for o in self._recent(days) if orders.is_store_order(o)]
                store.sort(key=lambda o: o.get("created_at") or "", reverse=True)
                picked.extend(store[:latest])
            rows = orders.import_orders(picked, dry_run=dry_run)
        except square.SquareError as exc:
            raise CommandError(str(exc)) from exc
        for row in rows:
            self.stdout.write(
                f"#{row['number']}  {row['source']}  {row['state']}  {row['total']}"
                f"  → {row['result']}"
            )
        note = " (dry run: nothing saved)" if dry_run else ""
        self.stdout.write(f"{len(rows)} order(s){note}.")

    def _recent(self, days):
        since = (timezone.now() - timedelta(days=days)).isoformat()
        locations = [settings.SQUARE_LOCATION_ID] if settings.SQUARE_LOCATION_ID else None
        for loc in locations or square.list_locations():
            yield from square.search_orders(loc, updated_since=since, max_pages=20)

    def _list(self, days):
        counts = {}
        shown = 0
        for order in self._recent(days):
            source = orders.source_name(order) or "(no source)"
            counts[source] = counts.get(source, 0) + 1
            if shown < 15:
                shown += 1
                fields = orders.read_order(order)
                self.stdout.write(
                    f"{(order.get('created_at') or '')[:10]}  {source}  {fields['state']}  "
                    f"{square.money(fields['total_cents'], fields['currency'])}"
                    f"  {'STORE' if orders.is_store_order(order) else ''}"
                )
        self.stdout.write(f"Orders in the last {days} days, by source:")
        for source, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            mark = "  ← store" if orders.is_store_order({"source": {"name": source}}) else ""
            self.stdout.write(f"  {n:>5}  {source}{mark}")
        self.stdout.write(f"Store sources set: {', '.join(settings.SQUARE_STORE_SOURCES)}")
