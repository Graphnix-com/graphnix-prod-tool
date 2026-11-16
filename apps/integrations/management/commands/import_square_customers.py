"""Go-live: tracker customers for everyone invoiced in Square in the last 2 years (FR-08).

    python manage.py import_square_customers --dry-run   # counts only
    python manage.py import_square_customers
    python manage.py import_square_customers --live --location L...   # staging: real account

Safe to run again: customers already linked are skipped. Square is only read.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.integrations import nightly, square

from . import _live


class Command(BaseCommand):
    help = "Create or link tracker customers for Square customers invoiced recently."

    def add_arguments(self, parser):
        parser.add_argument("--years", type=int, default=2)
        parser.add_argument("--dry-run", action="store_true")
        _live.add_arguments(parser)

    def handle(self, *args, years, dry_run, live, location, **options):
        _live.apply(live, location)
        if not square.api_configured():
            raise CommandError("Square isn't set up here (SQUARE_ACCESS_TOKEN).")
        try:
            nightly.check_location()
            counts = nightly.import_customers(years=years, dry_run=dry_run, progress=self._say)
        except square.SquareError as exc:
            raise CommandError(str(exc)) from exc
        prefix = "Would" if dry_run else "Done:"
        self.stdout.write(
            f"{prefix} {counts['square_customers']} Square customers invoiced in {years} years: "
            f"{counts['already_linked']} already linked, {counts['matched']} matched by email, "
            f"{counts['created']} {'to create' if dry_run else 'created'}."
        )

    def _say(self, text):
        self.stdout.write(text)
        self.stdout.flush()
