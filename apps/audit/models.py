from django.conf import settings
from django.db import models
from django.utils import timezone


class AuditLog(models.Model):
    """Every edit and override: who, when, what changed (NFR-03). Rows are never edited."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Empty means the system did it.",
    )
    entity_type = models.CharField(max_length=100)  # e.g. "jobs.job"
    entity_id = models.BigIntegerField()
    action = models.CharField(max_length=30)  # create, update, status_change, override, delete
    changes = models.JSONField(null=True, blank=True)  # {field: [old, new]}
    ip = models.GenericIPAddressField(null=True, blank=True)
    # The job this entry belongs to (its items, tasks, notes), for the job's timeline. A plain
    # id, not a foreign key, so entries outlive whatever they describe.
    job_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "audit_log"
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["entity_type", "entity_id", "at"])]

    def __str__(self) -> str:
        return f"{self.entity_type}#{self.entity_id} {self.action} @ {self.at:%Y-%m-%d %H:%M}"
