"""Read Square's estimate emails now, instead of waiting for the 5-minute check.

python manage.py check_estimate_mail             # since the last one read
python manage.py check_estimate_mail --days 30   # go back further (safe: each email once)
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.integrations import estimates


class Command(BaseCommand):
    help = "Read Square's estimate emails from ESTIMATE_MAILBOX."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, help="Look back this many days.")

    def handle(self, *args, days, **options):
        if not estimates.enabled():
            raise CommandError(
                "Estimate emails aren't set up here (ESTIMATE_MAILBOX and the M365 settings)."
            )
        since = timezone.now() - timedelta(days=days) if days else None
        result = estimates.check_mail(since=since)
        if result["error"]:
            raise CommandError(result["error"])
        self.stdout.write(f"Read {result['read']} new estimate email(s).")
