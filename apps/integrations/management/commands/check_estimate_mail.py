"""Read Square's estimate emails now, instead of waiting for the 5-minute check.

python manage.py check_estimate_mail             # since the last one read
python manage.py check_estimate_mail --days 30   # go back further (safe: each email once)
python manage.py check_estimate_mail --days 14 --dry-run   # what it would do; saves nothing
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.integrations import estimates


class Command(BaseCommand):
    help = "Read Square's estimate emails from ESTIMATE_MAILBOX."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, help="Look back this many days.")
        parser.add_argument(
            "--dry-run", action="store_true", help="Show what would happen; save nothing."
        )

    def handle(self, *args, days, dry_run, **options):
        if not estimates.enabled():
            raise CommandError(
                "Estimate emails aren't set up here (ESTIMATE_MAILBOX and the M365 settings)."
            )
        since = timezone.now() - timedelta(days=days) if days else None
        if dry_run:
            return self._preview(since)
        result = estimates.check_mail(since=since)
        if result["error"]:
            raise CommandError(result["error"])
        self.stdout.write(f"Read {result['read']} new estimate email(s).")

    def _preview(self, since):
        from apps.m365.graph import GraphError

        try:
            rows = estimates.preview(since=since)
        except GraphError as exc:
            raise CommandError(str(exc)) from exc
        for row in rows:
            what = f"#{row['number']} {row['kind']}" if row["number"] else ""
            self.stdout.write(f"{row['received']}  {what:<20} → {row['result']}")
        counts = {}
        for row in rows:
            key = row["result"]
            key = "existing job" if key.startswith("existing job") else key
            counts[key] = counts.get(key, 0) + 1
        summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        self.stdout.write(f"{len(rows)} email(s): {summary or 'none'}. Dry run: nothing saved.")
