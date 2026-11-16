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
    QUOTE_SENT = "quote_sent", "Quote Sent"
    # Retired: merged into Invoiced (Square invoices an accepted estimate at once). No job
    # can reach it; kept so older history still reads "Quote Approved".
    QUOTE_APPROVED = "quote_approved", "Quote Approved"
    INVOICED = "invoiced", "Invoiced"
    # Retired: Invoiced goes straight to Mock-up Sent. No job can reach it; kept so older
    # history still reads "Mock-up In Progress".
    MOCKUP_IN_PROGRESS = "mockup_in_progress", "Mock-up In Progress"
    MOCKUP_SENT = "mockup_sent", "Mock-up Sent"
    MOCKUP_APPROVED = "mockup_approved", "Mock-up Approved"
    MATERIALS_ORDERED = "materials_ordered", "Materials Ordered"
    READY_FOR_PRODUCTION = "ready_for_production", "Ready For Production"
    IN_PRODUCTION = "in_production", "In Production"
    QA = "qa", "QA"
    READY = "ready", "Ready"
    SHIPPED = "shipped", "Shipped / Picked Up"
    CLOSED = "closed", "Closed"
    ON_HOLD = "on_hold", "On Hold"
    CANCELLED = "cancelled", "Cancelled"


class PaymentStatus(models.TextChoices):
    """Set from Square webhooks in Step 2 (FR-05); never typed in."""

    UNPAID = "unpaid", "Unpaid"
    DEPOSIT = "deposit", "Deposit"
    PAID = "paid", "Paid"
    REFUNDED = "refunded", "Refunded"
    CANCELLED = "cancelled", "Cancelled"


class HoldReason(models.TextChoices):
    MOCKUP_CHANGE = "mockup_change", "Customer Changing Mock-up"
    PRODUCT_DELAY = "product_delay", "Product Delay"
    OTHER = "other", "Other"


class Priority(models.IntegerChoices):
    SOONER = 1, "Sooner If Possible"
    NORMAL = 2, "Normal"


class WaitingOn(models.TextChoices):
    GRAPHNIX = "graphnix", "Waiting On GRAPHNIX"
    CUSTOMER = "customer", "Waiting On Customer"


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
        "sales.Inquiry",
        verbose_name="request",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="job",
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
                violation_error_message="A job with this Square job number already exists.",
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
    def balance_cents(self) -> int | None:
        """What Square says is still owed (money: hidden from Production)."""
        if self.amount_total_cents is None:
            return None
        return self.amount_total_cents - self.amount_paid_cents

    @property
    def online_order(self) -> str:
        """ "New Online Order" for a store order nobody has moved on from Mock-Up Approved
        (where it arrives, already paid, past quoting and invoicing); "Online Order" after
        that; "" for any other job."""
        if self.source != Channel.ONLINE_STORE:
            return ""
        return "New Online Order" if self.status == Status.MOCKUP_APPROVED else "Online Order"

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


ONE_SIZE = "OS"
# The size grid on the item form, in display order. Other sizes can be typed in.
STANDARD_SIZES = [
    "YXS", "YS", "YM", "YL", "YXL",
    "XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL",
]  # fmt: skip


class JobItem(models.Model):
    """A line on the job: a garment with sizes, or a non-garment (mug, banner) with a quantity."""

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="items")
    line_no = models.PositiveSmallIntegerField(editable=False)
    item_no = models.CharField("item no.", max_length=60, blank=True)  # plan: style_code
    description = models.CharField(max_length=200)  # brand and style, e.g. "Gildan 5000 tee"
    color = models.CharField("colour", max_length=60, blank=True)
    # False for towels, mugs, knives, pens, signs, banners, stickers, wraps: one "OS" size row
    # holds the quantity, so totals work the same for every item.
    is_sized = models.BooleanField("has sizes", default=True)
    customer_supplied = models.BooleanField(default=False)
    customer_goods_received_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "job_item"
        ordering = ["job", "line_no"]
        constraints = [
            models.UniqueConstraint(fields=["job", "line_no"], name="job_item_line_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.line_no}. {self.description}"

    def save(self, *args, **kwargs):
        if not self.line_no:
            last = JobItem.objects.filter(job=self.job_id).aggregate(n=models.Max("line_no"))
            self.line_no = (last["n"] or 0) + 1
        super().save(*args, **kwargs)

    @property
    def total_quantity(self) -> int:
        return sum(s.quantity for s in self.sizes.all())


class JobItemSize(models.Model):
    job_item = models.ForeignKey(JobItem, on_delete=models.CASCADE, related_name="sizes")
    size = models.CharField(max_length=10)
    quantity = models.PositiveIntegerField()

    class Meta:
        db_table = "job_item_size"
        constraints = [
            models.UniqueConstraint(fields=["job_item", "size"], name="job_item_size_unique"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="job_item_size_qty_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.size} × {self.quantity}"

    @property
    def sort_key(self):
        if self.size in STANDARD_SIZES:
            return (0, STANDARD_SIZES.index(self.size))
        return (1, self.size)


def _job_file_path(instance, filename: str) -> str:
    # Unguessable and unique; the name people see is kept in `name`.
    import uuid

    from django.utils.text import get_valid_filename

    return f"job_files/{instance.job_id}/{uuid.uuid4().hex}-{get_valid_filename(filename)[-80:]}"


class JobFile(models.Model):
    """Artwork, mock-ups, photos and PDFs on a job. Everyone who can open the job sees them,
    Production included, so no quotes or invoices here (see apps.jobs.files)."""

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="files")
    file = models.FileField(upload_to=_job_file_path, max_length=255)
    name = models.CharField(max_length=200)
    size = models.PositiveBigIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "job_file"
        ordering = ["-uploaded_at", "-id"]

    def __str__(self) -> str:
        return self.name

    @property
    def extension(self) -> str:
        return self.name.rsplit(".", 1)[-1].lower() if "." in self.name else ""

    @property
    def is_image(self) -> bool:
        """Shown as a thumbnail and opened in the browser (not SVG: it can carry script)."""
        return self.extension in {"png", "jpg", "jpeg", "gif", "webp"}
