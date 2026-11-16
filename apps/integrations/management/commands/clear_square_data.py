"""Staging only: remove everything that came from Square (real or sandbox) once testing is done.

    python manage.py clear_square_data          # shows what would go; nothing is removed
    python manage.py clear_square_data --yes    # removes it

Refused on production. Square itself is never touched. See apps/integrations/cleanup.py.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.integrations import cleanup

LABELS = {
    "customers": "customers from Square (with their contacts and addresses)",
    "jobs": "jobs for those customers, store orders or estimates",
    "invoices": "Square invoices",
    "orders": "store orders",
    "estimates": "estimates",
    "estimate_emails": "estimate emails read",
}


class Command(BaseCommand):
    help = "Staging only: remove the customers, jobs and records imported from Square."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Remove them (else a preview).")

    def handle(self, *args, yes, **options):
        if settings.APP_ENV == "production":
            raise CommandError("Refused: this is production. Square data here is the real record.")
        p = cleanup.plan()
        numbers = [j.display_number for j in p["jobs"].order_by("pk")[:30]]
        found = cleanup.counts(p) if not yes else cleanup.clear()
        verb = "Removed" if yes else "Would remove"
        self.stdout.write(f"{verb} ({settings.APP_ENV}):")
        for name, label in LABELS.items():
            self.stdout.write(f"  {found[name]:>6}  {label}")
        self.stdout.write(
            f"Kept: {found['unlinked_jobs']} staff job(s) linked to Square, unlinked and their "
            "payment reset."
        )
        if numbers:
            more = " …" if found["jobs"] > len(numbers) else ""
            self.stdout.write(f"Jobs: {', '.join(numbers)}{more}")
        if not yes:
            self.stdout.write("Nothing removed. Run again with --yes to remove these.")
