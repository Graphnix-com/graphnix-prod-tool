"""Read the website's contact form emails now, instead of waiting for the 5-minute check.

python manage.py check_webform_mail             # since the last one read
python manage.py check_webform_mail --days 30   # go back further (safe: each email once)
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.sales import webform


class Command(BaseCommand):
    help = "Make requests from the website's contact form emails in WEBFORM_MAILBOX."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, help="Look back this many days.")

    def handle(self, *args, days, **options):
        if not webform.enabled():
            raise CommandError(
                "Website form emails aren't set up here (WEBFORM_MAILBOX or ESTIMATE_MAILBOX, "
                "and the M365 settings)."
            )
        since = timezone.now() - timedelta(days=days) if days else None
        result = webform.check_mail(since=since)
        if result["error"]:
            raise CommandError(result["error"])
        self.stdout.write(f"Made {result['read']} new request(s) from the website form.")
