from django.conf import settings
from django.db import models, transaction


class DecorationMethod(models.Model):
    """Screen print, embroidery, heat press... (FR-20). Admins add more from the task form."""

    code = models.SlugField(max_length=30, unique=True)  # SCREEN, EMB, HEAT ...
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "decoration_method"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        # Each method has exactly one station today (decided 7 Nov 2026). It is created with
        # the method and kept out of the screens, so Step 3's queues work unchanged when a
        # second machine arrives.
        with transaction.atomic():
            super().save(*args, **kwargs)
            if not self.stations.exists():
                Station.objects.create(name=self.name, decoration_method=self)


class Station(models.Model):
    """A machine or bench that works one method's queue (Step 3)."""

    name = models.CharField(max_length=100, unique=True)
    decoration_method = models.ForeignKey(
        DecorationMethod, on_delete=models.PROTECT, related_name="stations"
    )
    daily_capacity_units = models.PositiveIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "station"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class TaskStatus(models.TextChoices):
    """Task states for Step 3's station queues. Step 1 creates tasks as Planned."""

    BLOCKED = "blocked", "Planned"
    QUEUED = "queued", "In Queue"
    IN_PROGRESS = "in_progress", "In Progress"
    PAUSED = "paused", "Paused"
    DONE = "done", "Done"
    REWORK = "rework", "Rework"


class ProductionTask(models.Model):
    """One decoration on one item (FR-21): a job printed on the front and embroidered on the
    sleeve has two tasks, in two station queues."""

    job = models.ForeignKey("jobs.Job", on_delete=models.CASCADE, related_name="tasks")
    job_item = models.ForeignKey(
        "jobs.JobItem", on_delete=models.CASCADE, related_name="tasks", null=True, blank=True
    )
    decoration_method = models.ForeignKey(
        DecorationMethod, on_delete=models.PROTECT, related_name="tasks", verbose_name="method"
    )
    station = models.ForeignKey(
        Station, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks"
    )
    placement = models.CharField(max_length=100, blank=True)  # "front", "left chest"
    # Details are free text (decided 7 Nov 2026): ink colours, stitch count, transfer size...
    notes = models.TextField(blank=True)
    # Done by a supplier; Step 3 links the task to a purchase order.
    outsourced = models.BooleanField(default=False)
    # Kept for fixed per-method fields later; empty in Step 1.
    spec = models.JSONField(default=dict, blank=True)
    sequence = models.PositiveSmallIntegerField(default=1)
    depends_on = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(max_length=20, choices=TaskStatus.choices, default=TaskStatus.BLOCKED)
    queue_rank = models.DecimalField(max_digits=20, decimal_places=10, null=True, blank=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tasks",
    )
    scheduled_date = models.DateField(null=True, blank=True)
    qty_planned = models.PositiveIntegerField(default=0)
    qty_done = models.PositiveIntegerField(default=0)
    qty_spoiled = models.PositiveIntegerField(default=0)
    rework_reason = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "production_task"
        ordering = ["job", "sequence", "id"]
        permissions = [
            ("assign_any_task", "Can assign tasks to anyone"),
            ("assign_staff_task", "Can assign tasks to anyone but Admin/Owner"),
            ("take_task", "Can take a task for themselves"),
        ]
        indexes = [
            models.Index(fields=["station", "status", "queue_rank"]),
            models.Index(fields=["assigned_to", "status"]),
        ]

    def __str__(self) -> str:
        where = f" ({self.placement})" if self.placement else ""
        return f"{self.decoration_method}{where}"

    def save(self, *args, **kwargs):
        if self.station_id is None:
            self.station = self.decoration_method.stations.filter(is_active=True).first()
        super().save(*args, **kwargs)
