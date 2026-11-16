"""Square, read-only (Step 2): webhook deliveries and a copy of each invoice.

Square is the system of record for money. Nothing here is typed in: rows are written by
`apps.integrations.square` from Square's own data, except who linked an invoice to a job.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


class WebhookEvent(models.Model):
    """Every signed delivery, stored raw before anything else, once per Square event ID.
    Holds customer names and emails, so rows are deleted after 90 days (privacy checklist)."""

    event_id = models.CharField(max_length=100, unique=True)
    event_type = models.CharField(max_length=60)
    square_object_id = models.CharField(max_length=100, blank=True, db_index=True)
    body = models.JSONField()
    received_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)

    class Meta:
        db_table = "webhook_event"
        ordering = ["-received_at"]

    def __str__(self) -> str:
        return f"{self.event_type} {self.event_id}"


class SquareInvoice(models.Model):
    """The latest copy of one Square invoice, and the job it belongs to."""

    class Match(models.TextChoices):
        TITLE = "title", "Job number in the invoice"
        MANUAL = "manual", "Linked by hand"
        UNLINKED = "unlinked", "Unlinked by hand"
        ESTIMATE = "estimate", "From the accepted estimate"

    square_id = models.CharField(max_length=100, unique=True)
    version = models.IntegerField(default=0)
    location_id = models.CharField(max_length=100, blank=True)
    order_id = models.CharField(max_length=100, blank=True)
    invoice_number = models.CharField(max_length=100, blank=True)
    title = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=30)  # Square's own: DRAFT, UNPAID, PAID, ...
    square_customer_id = models.CharField(max_length=100, blank=True)
    recipient_name = models.CharField(max_length=255, blank=True)
    currency = models.CharField(max_length=3, default="CAD")
    total_cents = models.BigIntegerField(null=True, blank=True)
    paid_cents = models.BigIntegerField(default=0)
    refunded_cents = models.BigIntegerField(default=0)
    due_date = models.DateField(null=True, blank=True)
    public_url = models.URLField(max_length=500, blank=True)
    square_updated_at = models.DateTimeField(null=True, blank=True)

    job = models.ForeignKey(
        "jobs.Job",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="square_invoices",
    )
    match_method = models.CharField(max_length=10, choices=Match.choices, blank=True)
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    linked_at = models.DateTimeField(null=True, blank=True)
    not_a_job_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    not_a_job_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "square_invoice"
        ordering = ["-square_updated_at", "-id"]
        permissions = [
            ("link_square_invoice", "Can link Square invoices to jobs"),
            ("override_payment_gate", "Can move a job past an unmet payment gate"),
        ]

    def __str__(self) -> str:
        return f"Invoice {self.invoice_number or self.square_id}"

    @property
    def status_label(self) -> str:
        """Square's PARTIALLY_PAID as "Partially paid"."""
        return self.status.replace("_", " ").capitalize()

    @property
    def sent(self) -> bool:
        return self.status not in ("DRAFT", "")


class SquareOrder(models.Model):
    """The latest copy of one online store order (Square Online), and the job made from it.

    Store orders skip the quote and invoice: paid at checkout, with a store design that is
    already approved, so each becomes a job at Mock-Up Approved. Counter sales are Square
    orders too; they're never stored (only sources in SQUARE_STORE_SOURCES are)."""

    square_id = models.CharField(max_length=100, unique=True)
    version = models.IntegerField(default=0)
    location_id = models.CharField(max_length=100, blank=True)
    source_name = models.CharField(max_length=255, blank=True)
    reference = models.CharField(max_length=100, blank=True)  # the store's order number
    state = models.CharField(max_length=20)  # Square's own: OPEN, COMPLETED, CANCELED, DRAFT
    square_customer_id = models.CharField(max_length=100, blank=True)
    buyer_name = models.CharField(max_length=255, blank=True)
    fulfillment = models.CharField(max_length=20, blank=True)  # SHIPMENT, PICKUP, DELIVERY
    currency = models.CharField(max_length=3, default="CAD")
    total_cents = models.BigIntegerField(null=True, blank=True)
    paid_cents = models.BigIntegerField(default=0)
    refunded_cents = models.BigIntegerField(default=0)
    square_created_at = models.DateTimeField(null=True, blank=True)
    square_updated_at = models.DateTimeField(null=True, blank=True)
    job = models.ForeignKey(
        "jobs.Job",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="square_orders",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "square_order"
        ordering = ["-square_created_at", "-id"]

    def __str__(self) -> str:
        return f"Store order {self.number}"

    @property
    def number(self) -> str:
        return self.reference or self.square_id[-6:]

    @property
    def status(self) -> str:
        """Read like an invoice's status by the payment summary."""
        return self.state

    @property
    def status_label(self) -> str:
        return {"CANCELED": "Cancelled"}.get(self.state, self.state.capitalize())


class SquareEstimate(models.Model):
    """A Square estimate, known only from Square's emails: its API has no estimates.

    "A new estimate was sent to …" puts the job at Quote Sent; "You accepted an estimate!"
    puts it at Invoiced. The estimate number becomes the job's Square number."""

    class Match(models.TextChoices):
        NUMBER = "number", "Estimate number already on the job"
        CUSTOMER = "customer", "The customer's one quoting job"
        CREATED = "created", "New job for the estimate"
        MANUAL = "manual", "Linked by hand"

    number = models.CharField(max_length=40, unique=True)
    customer_name = models.CharField(max_length=255, blank=True)
    customer_email = models.EmailField(blank=True)
    currency = models.CharField(max_length=3, default="CAD")
    total_cents = models.BigIntegerField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    job = models.ForeignKey(
        "jobs.Job",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="square_estimates",
    )
    match_method = models.CharField(max_length=10, choices=Match.choices, blank=True)
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    not_a_job_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    not_a_job_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "square_estimate"
        ordering = ["-sent_at", "-id"]

    def __str__(self) -> str:
        return f"Estimate {self.number}"

    @property
    def state(self) -> str:
        return "Accepted" if self.accepted_at else "Sent"


class EstimateEmail(models.Model):
    """Each Square estimate email read from the mailbox, once. Only what was parsed is
    kept, not the email itself."""

    class Kind(models.TextChoices):
        SENT = "sent", "Estimate sent"
        ACCEPTED = "accepted", "Estimate accepted"
        OTHER = "other", "Not an estimate email"

    message_id = models.CharField(max_length=255, unique=True)  # Graph's immutable ID
    received_at = models.DateTimeField()
    kind = models.CharField(max_length=10, choices=Kind.choices)
    estimate = models.ForeignKey(
        SquareEstimate, null=True, blank=True, on_delete=models.SET_NULL, related_name="emails"
    )
    error = models.TextField(blank=True)
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "estimate_email"
        ordering = ["-received_at"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} email {self.received_at:%Y-%m-%d %H:%M}"
