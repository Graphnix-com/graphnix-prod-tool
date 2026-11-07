"""Customers: mostly individuals, sometimes a company with one or two contacts.

Holds only what a job needs (NFR-09, Alberta PIPA): name, company, email, phone, addresses.
"""

from django.db import models
from django.db.models import Q


class Customer(models.Model):
    display_name = models.CharField("name", max_length=200)
    company_name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    # Filled by the Square customer sync in Step 2 (FR-08).
    square_customer_id = models.CharField(max_length=64, blank=True)
    notifications_opt_out = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "customer"
        ordering = ["display_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["square_customer_id"],
                condition=~Q(square_customer_id=""),
                name="customer_square_id_unique",
            ),
        ]
        permissions = [("view_contact_details", "Can see customer contact details")]

    def __str__(self) -> str:
        if self.company_name and self.company_name != self.display_name:
            return f"{self.display_name} ({self.company_name})"
        return self.display_name


class Contact(models.Model):
    """A person at a company customer. Individuals need none: their details are on Customer."""

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="contacts")
    full_name = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    is_primary = models.BooleanField(default=False)
    receives_notifications = models.BooleanField(default=True)

    class Meta:
        db_table = "contact"
        ordering = ["-is_primary", "full_name"]

    def __str__(self) -> str:
        return self.full_name


class Address(models.Model):
    class Kind(models.TextChoices):
        BILLING = "billing", "Billing"
        SHIPPING = "shipping", "Shipping"

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="addresses")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.SHIPPING)
    line1 = models.CharField(max_length=200)
    line2 = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100)
    province = models.CharField(max_length=2, default="AB")
    postal_code = models.CharField(max_length=10)
    country = models.CharField(max_length=2, default="CA")

    class Meta:
        db_table = "address"
        verbose_name_plural = "addresses"

    def __str__(self) -> str:
        return f"{self.line1}, {self.city}"
