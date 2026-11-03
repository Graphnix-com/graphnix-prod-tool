from django.utils import timezone
from procrastinate.contrib.django import app

from .models import Heartbeat


@app.periodic(cron="* * * * *")
@app.task(queue="default")
def worker_heartbeat(timestamp: int) -> None:
    """Runs every minute. The worker's Docker health check fails if this goes stale."""
    Heartbeat.objects.update_or_create(name="worker", defaults={"last_seen": timezone.now()})
