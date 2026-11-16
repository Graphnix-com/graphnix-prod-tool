"""Copy a few Square invoices into the tracker, as if their webhooks had arrived.

    python manage.py import_square_invoices --latest 3 --dry-run
    python manage.py import_square_invoices --number 000123 --number 000124
    python manage.py import_square_invoices --id inv:0-ChA...
    python manage.py import_square_invoices --latest 3 --live --location L...   # real account

For trying real invoices on staging. An invoice with a job's G- number links itself; the rest
wait under Invoices to be linked by hand. Safe to run again. Square is only read.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.integrations import nightly, square

from . import _live


class Command(BaseCommand):
    help = "Import chosen Square invoices (by ID, by number, or the latest few)."

    def add_arguments(self, parser):
        parser.add_argument("--id", action="append", default=[], dest="ids")
        parser.add_argument("--number", action="append", default=[], dest="numbers")
        parser.add_argument("--latest", type=int, default=0)
        parser.add_argument("--dry-run", action="store_true")
        _live.add_arguments(parser)

    def handle(self, *args, ids, numbers, latest, dry_run, live, location, **options):
        _live.apply(live, location)
        if not square.api_configured():
            raise CommandError("Square isn't set up here (SQUARE_ACCESS_TOKEN).")
        if not (ids or numbers or latest):
            raise CommandError("Say which invoices: --id, --number or --latest N.")
        if latest > 20:
            raise CommandError("--latest is for a few samples: 20 at most.")
        try:
            nightly.check_location()
            rows = nightly.import_invoices(
                nightly.pick_invoices(ids=ids, numbers=numbers, latest=latest), dry_run=dry_run
            )
        except square.SquareError as exc:
            raise CommandError(str(exc)) from exc
        for row in rows:
            self.stdout.write(
                f"#{row['number']}  {row['title'] or '(no title)'}  {row['status']}  "
                f"{row['total']}  → {row['result']}"
            )
        self.stdout.write(
            f"{len(rows)} invoice(s){' (dry run: nothing saved)' if dry_run else ''}."
        )
