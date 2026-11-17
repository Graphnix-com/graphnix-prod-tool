"""Purchase orders for blanks, consumables and outsourced work (FR-13 to FR-15).

The tracker records what was ordered for which jobs and what has arrived; choosing a
supplier and placing the order stay manual. No supplier list and no prices (Step 3 spec,
decided 17 Nov 2026), so anyone who can open a job may see its POs.
"""

from django.conf import settings
from django.db import connection, models
from django.db.models import Q
from django.utils import timezone

PO_PREFIX = "PO-"


class PurchaseOrder(models.Model):
    class Kind(models.TextChoices):
        GARMENT = "garment", "Garments"
        CONSUMABLE = "consumable", "Consumables"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ORDERED = "ordered", "Ordered"
        PARTLY_RECEIVED = "partly_received", "Partly Received"
        RECEIVED = "received", "Received"
        CANCELLED = "cancelled", "Cancelled"

    po_number = models.CharField("PO number", max_length=20, unique=True, editable=False)
    # Who the order is with, typed: there is no supplier list.
    supplier_name = models.CharField("supplier", max_length=200)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.GARMENT)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    supplier_order_ref = models.CharField("supplier's order number", max_length=100, blank=True)
    ordered_at = models.DateTimeField(null=True, blank=True)
    expected_at = models.DateField("expected", null=True, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "purchase_order"
        ordering = ["-created_at", "-id"]
        verbose_name = "purchase order"
        permissions = [("receive_purchaseorder", "Can receive deliveries on a purchase order")]

    def __str__(self) -> str:
        return f"{self.po_number} {self.supplier_name}"

    def save(self, *args, **kwargs):
        if not self.po_number:
            with connection.cursor() as cursor:
                cursor.execute("SELECT nextval('po_number_seq')")
                self.po_number = f"{PO_PREFIX}{cursor.fetchone()[0]}"
        super().save(*args, **kwargs)

    @property
    def is_open(self) -> bool:
        return self.status in (self.Status.ORDERED, self.Status.PARTLY_RECEIVED)

    @property
    def is_late(self) -> bool:
        return bool(self.is_open and self.expected_at and self.expected_at < timezone.localdate())


class PurchaseOrderLine(models.Model):
    """One item, colour and size for one job; or a consumable (for a job, or for stock when
    `job` is empty); or an outsourced task, done when received."""

    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.CASCADE, related_name="lines"
    )
    job = models.ForeignKey(
        "jobs.Job", null=True, blank=True, on_delete=models.PROTECT, related_name="po_lines"
    )
    job_item = models.ForeignKey(
        "jobs.JobItem", null=True, blank=True, on_delete=models.SET_NULL, related_name="po_lines"
    )
    production_task = models.ForeignKey(
        "production.ProductionTask",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="po_lines",
    )
    description = models.CharField(max_length=200)
    item_no = models.CharField("item no.", max_length=60, blank=True)
    color = models.CharField("colour", max_length=60, blank=True)
    size = models.CharField(max_length=10, blank=True)
    qty_ordered = models.PositiveIntegerField()
    qty_received = models.PositiveIntegerField(default=0)
    closed_short_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        db_table = "purchase_order_line"
        ordering = ["job_id", "job_item_id", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(qty_ordered__gt=0), name="po_line_qty_positive"),
        ]

    def __str__(self) -> str:
        bits = [self.description, self.color, self.size]
        return " · ".join(b for b in bits if b)

    @property
    def outstanding(self) -> int:
        return max(self.qty_ordered - self.qty_received, 0)

    @property
    def is_complete(self) -> bool:
        return self.qty_received >= self.qty_ordered or bool(self.closed_short_reason)


class Receipt(models.Model):
    """What arrived, by line: one row per delivery entered."""

    line = models.ForeignKey(PurchaseOrderLine, on_delete=models.CASCADE, related_name="receipts")
    qty = models.PositiveIntegerField()
    received_at = models.DateTimeField(default=timezone.now)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        db_table = "po_receipt"
        ordering = ["-received_at", "-id"]

    def __str__(self) -> str:
        return f"{self.qty} received"
