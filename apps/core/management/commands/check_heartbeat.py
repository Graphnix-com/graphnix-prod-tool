from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.core.models import Heartbeat


class Command(BaseCommand):
    help = "Exit non-zero if a heartbeat is missing or older than --max-age seconds."

    def add_arguments(self, parser):
        parser.add_argument("name")
        parser.add_argument("--max-age", type=int, default=180)

    def handle(self, *args, name, max_age, **options):
        beat = Heartbeat.objects.filter(name=name).first()
        if beat is None:
            raise CommandError(f"No heartbeat recorded for {name!r}")
        age = (timezone.now() - beat.last_seen).total_seconds()
        if age > max_age:
            raise CommandError(f"{name!r} heartbeat is {int(age)} s old (limit {max_age} s)")
        self.stdout.write(f"{name} ok ({int(age)} s)")
