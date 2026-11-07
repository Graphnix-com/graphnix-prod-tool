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
