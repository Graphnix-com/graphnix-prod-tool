from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Channel(models.TextChoices):
    """Where a request came from (FR-01). Online store orders may skip the mock-up."""

    EMAIL = "email", "Email"
    PHONE = "phone", "Phone"
    WALK_IN = "walk_in", "Walk-in"
    ONLINE_STORE = "online_store", "Online Store"
    WEBSITE = "website", "Website Form"
    OTHER = "other", "Other"


class Inquiry(models.Model):
    """A quote request, logged before a job exists."""

    class Status(models.TextChoices):
        NEW = "new", "New"
        IN_REVIEW = "in_review", "In Review"
        CONVERTED = "converted", "Converted To Job"
        DECLINED = "declined", "Declined"

    channel = models.CharField(max_length=20, choices=Channel.choices)
    received_at = models.DateTimeField(default=timezone.now)
    customer = models.ForeignKey(
        "customers.Customer",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inquiries",
    )
    contact = models.ForeignKey(
        "customers.Contact", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # What we were told before the customer is matched or created.
    from_name = models.CharField(max_length=200, blank=True)
    from_email = models.EmailField(blank=True)
    from_phone = models.CharField(max_length=40, blank=True)
    subject = models.CharField(max_length=200, blank=True)
    details = models.TextField(blank=True)
    # Set when Step 4 captures the request from the quote mailbox (INT-03).
    graph_message_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inquiry"
        verbose_name = "request"
        verbose_name_plural = "requests"
        ordering = ["received_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["graph_message_id"],
                condition=~Q(graph_message_id=""),
                name="inquiry_graph_message_unique",
            ),
        ]

    def __str__(self) -> str:
        who = self.customer or self.from_name or self.from_email or "unknown"
        return f"{self.get_channel_display()} from {who}, {self.received_at:%Y-%m-%d}"
