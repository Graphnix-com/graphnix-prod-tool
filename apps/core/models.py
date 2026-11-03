from django.db import models


class Heartbeat(models.Model):
    """Last time a background process proved it was alive (e.g. the worker, once a minute)."""

    name = models.CharField(max_length=50, primary_key=True)
    last_seen = models.DateTimeField()

    class Meta:
        db_table = "heartbeat"
        permissions = [("view_system_health", "Can view the system health panel")]

    def __str__(self) -> str:
        return f"{self.name} @ {self.last_seen:%Y-%m-%d %H:%M:%S}"
