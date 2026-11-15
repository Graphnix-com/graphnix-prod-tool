"""Saving customers with their contacts and addresses, audited (NFR-03)."""

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.forms.models import model_to_dict

from apps.audit.services import diff, record

from .models import Customer


def _require(user, perm: str) -> None:
    if not user.has_perm(perm):
        raise PermissionDenied(f"You don't have permission to do that ({perm}).")


def _audit_formset(user, formset) -> None:
    for form in formset.deleted_forms:
        if form.instance.pk:
            record(user, form.instance, "delete")
    edits = [
        (f, {k: [f.initial.get(k), f.cleaned_data.get(k)] for k in f.changed_data})
        for f in formset.forms
        if f.instance.pk and f.has_changed() and f not in formset.deleted_forms
    ]
    formset.save()
    for form, changes in edits:
        record(user, form.instance, "update", changes)
    for obj in formset.new_objects:
        record(user, obj, "create")


@transaction.atomic
def save_customer(*, user, form, contacts, addresses) -> Customer:
    """Save valid forms for a customer and their contacts and addresses."""
    creating = form.instance.pk is None
    _require(user, "customers.add_customer" if creating else "customers.change_customer")
    before = {} if creating else model_to_dict(Customer.objects.get(pk=form.instance.pk))
    customer = form.save()
    if creating:
        record(user, customer, "create", {"display_name": [None, customer.display_name]})
    elif changes := diff(before, model_to_dict(customer)):
        record(user, customer, "update", changes)
    for formset in (contacts, addresses):
        formset.instance = customer
        _audit_formset(user, formset)
    return customer


@transaction.atomic
def customer_from_inquiry(inquiry, *, user) -> Customer:
    """A new customer from what the inquiry says, linked to it."""
    _require(user, "customers.add_customer")
    customer = Customer.objects.create(
        display_name=inquiry.from_name or inquiry.from_email or "New customer",
        email=inquiry.from_email,
        phone=inquiry.from_phone,
    )
    record(user, customer, "create", {"display_name": [None, customer.display_name]})
    set_inquiry_customer(inquiry, customer, user=user)
    return customer


def set_inquiry_customer(inquiry, customer, *, user) -> None:
    _require(user, "sales.change_inquiry")
    old = inquiry.customer_id
    inquiry.customer = customer
    if inquiry.status == inquiry.Status.NEW:
        inquiry.status = inquiry.Status.IN_REVIEW
    inquiry.save(update_fields=["customer", "status"])
    record(user, inquiry, "update", {"customer": [old, customer.pk]})
