"""The job: one per order, moving through a fixed state machine (FR-30).

Only `apps.jobs.services` may change `Job.status`; see the rules in `apps.jobs.flow`.
"""

from django.conf import settings
from django.db import connection, models
from django.db.models import Q
from django.utils import timezone

from apps.sales.models import Channel

JOB_NUMBER_PREFIX = "G-"


class Status(models.TextChoices):
    QUOTING = "quoting", "Quoting"
    QUOTE_SENT = "quote_sent", "Quote sent"
    QUOTE_APPROVED = "quote_approved", "Quote approved"
    INVOICED = "invoiced", "Invoiced"
    MOCKUP_IN_PROGRESS = "mockup_in_progress", "Mock-up in progress"
    MOCKUP_SENT = "mockup_sent", "Mock-up sent"
    MOCKUP_APPROVED = "mockup_approved", "Mock-up approved"
    MATERIALS_ORDERED = "materials_ordered", "Materials ordered"
    READY_FOR_PRODUCTION = "ready_for_production", "Ready for production"
    IN_PRODUCTION = "in_production", "In production"
    QA = "qa", "QA"
    READY = "ready", "Ready"
    SHIPPED = "shipped", "Shipped / picked up"
    CLOSED = "closed", "Closed"
    ON_HOLD = "on_hold", "On hold"
    CANCELLED = "cancelled", "Cancelled"


class PaymentStatus(models.TextChoices):
    """Set from Square webhooks in Step 2 (FR-05); never typed in."""

    UNPAID = "unpaid", "Unpaid"
    DEPOSIT = "deposit", "Deposit"
    PAID = "paid", "Paid"
    REFUNDED = "refunded", "Refunded"
    CANCELLED = "cancelled", "Cancelled"


class HoldReason(models.TextChoices):
    MOCKUP_CHANGE = "mockup_change", "Customer changing mock-up"
    PRODUCT_DELAY = "product_delay", "Product delay"
    OTHER = "other", "Other"


class Priority(models.IntegerChoices):
    SOONER = 1, "Sooner if possible"
    NORMAL = 2, "Normal"


class WaitingOn(models.TextChoices):
    GRAPHNIX = "graphnix", "Waiting on Graphnix"
    CUSTOMER = "customer", "Waiting on customer"


class Job(models.Model):
    # Internal key, G-1000 upwards, from a Postgres sequence so concurrent creates never
    # collide (FR-02). Staff see and search the Square number first.
    job_number = models.CharField(max_length=20, unique=True, editable=False)
    # The job ID Square generates; the same through estimate and invoice (to confirm on a
    # real job in Step 2). Step 2 links invoices to jobs by this number.
    square_number = models.CharField("Square job number", max_length=40, blank=True)
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="jobs"
    )
    contact = models.ForeignKey(
        "customers.Contact", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    inquiry = models.OneToOneField(
        "sales.Inquiry", null=True, blank=True, on_delete=models.SET_NULL, related_name="job"
    )
    title = models.CharField(max_length=200)
    source = models.CharField(max_length=20, choices=Channel.choices, blank=True)

    status = models.CharField(
        max_length=30, choices=Status.choices, default=Status.QUOTING, editable=False
    )
    status_changed_at = models.DateTimeField(default=timezone.now, editable=False)
    # The stage to return to after On hold, or after an Admin reopens a cancelled job.
    resume_status = models.CharField(
        max_length=30, choices=Status.choices, blank=True, editable=False
    )
    priority = models.PositiveSmallIntegerField(choices=Priority.choices, default=Priority.NORMAL)

    due_date = models.DateField(null=True, blank=True, help_text="Customer's in-hands date.")
    # Mock-up approval + the turnaround in business days; set again at each approval.
    suggested_due_date = models.DateField(null=True, blank=True, editable=False)
    square_estimate_ref = models.CharField(max_length=64, blank=True)
    quote_sent_at = models.DateTimeField(null=True, blank=True)
    quote_approved_at = models.DateTimeField(null=True, blank=True)
    mockup_approved_at = models.DateTimeField(null=True, blank=True, editable=False)

    # Money: copied from Square in Step 2, never typed in. Hidden from Production (NFR-02).
    payment_status = models.CharField(
        max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID, editable=False
    )
    amount_total_cents = models.BigIntegerField(null=True, blank=True, editable=False)
    amount_paid_cents = models.BigIntegerField(default=0, editable=False)
    currency = models.CharField(max_length=3, default="CAD", editable=False)

    fulfillment_method = models.CharField(
        max_length=10,
        choices=[("pickup", "Pickup"), ("ship", "Ship"), ("deliver", "Drop-off")],
        blank=True,
    )
    shared_drive_path = models.CharField("NAS folder", max_length=500, blank=True)
    hold_reason_code = models.CharField(
        max_length=20, choices=HoldReason.choices, blank=True, editable=False
    )
    hold_reason = models.TextField(blank=True, editable=False)
    cancel_reason = models.TextField(blank=True, editable=False)
    notifications_opt_out = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        db_table = "job"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "due_date"])]
        constraints = [
            models.UniqueConstraint(
                fields=["square_number"],
                condition=~Q(square_number=""),
                name="job_square_number_unique",
            ),
            models.CheckConstraint(condition=Q(status__in=Status.values), name="job_status_valid"),
        ]
        permissions = [
            ("move_office_stages", "Can move jobs through sales and pre-production"),
            ("move_production_stages", "Can move jobs through production"),
            ("move_finish_stages", "Can move jobs through QA and shipping"),
            ("override_job_flow", "Can move jobs backward, skip stages, hold and cancel"),
            ("reopen_cancelled_job", "Can reopen a cancelled job"),
            ("view_money", "Can see prices and payments"),
        ]

    def __str__(self) -> str:
        return f"{self.display_number} {self.title}"

    def save(self, *args, **kwargs):
        self.square_number = self.square_number.strip()
        if not self.job_number:
            with connection.cursor() as cursor:
                cursor.execute("SELECT nextval('job_number_seq')")
                self.job_number = f"{JOB_NUMBER_PREFIX}{cursor.fetchone()[0]}"
        super().save(*args, **kwargs)

    @property
    def display_number(self) -> str:
        """The number staff use: Square's once known, else the internal one."""
        return self.square_number or self.job_number

    @property
    def waiting_on(self) -> str | None:
        """Who must act next, for the Home page split. None once the job is finished."""
        if self.status in (Status.SHIPPED, Status.CLOSED, Status.CANCELLED):
            return None
        if self.status in (Status.QUOTE_SENT, Status.MOCKUP_SENT):
            return WaitingOn.CUSTOMER
        if self.status == Status.ON_HOLD and self.hold_reason_code == HoldReason.MOCKUP_CHANGE:
            return WaitingOn.CUSTOMER
        return WaitingOn.GRAPHNIX


class JobStatusHistory(models.Model):
    """One row per status change: the job's timeline and time-in-stage reports."""

    class Source(models.TextChoices):
        USER = "user", "Staff"
        SQUARE = "square", "Square"
        SYSTEM = "system", "System"
        CUSTOMER = "customer", "Customer"

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="history")
    from_status = models.CharField(max_length=30, choices=Status.choices, blank=True)
    to_status = models.CharField(max_length=30, choices=Status.choices)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.USER)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    note = models.TextField(blank=True)
    changed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "job_status_history"
        ordering = ["changed_at", "id"]
        indexes = [models.Index(fields=["job", "changed_at"])]
        verbose_name_plural = "job status history"

    def __str__(self) -> str:
        return f"{self.job.job_number}: {self.from_status or 'new'} → {self.to_status}"


class JobNote(models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="notes")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    body = models.TextField()
    is_internal = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "job_note"
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"Note on {self.job.job_number} by {self.author or 'system'}"
