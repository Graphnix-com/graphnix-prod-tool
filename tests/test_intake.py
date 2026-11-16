"""Inquiry and customer screens: log, match, convert, decline (FR-01, FR-02, NFR-09)."""

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.customers.models import Contact, Customer
from apps.jobs.models import Job, Status
from apps.sales.models import Channel, Inquiry

pytestmark = pytest.mark.django_db


def logged_in(client, user):
    client.force_login(user)
    return client


@pytest.mark.parametrize("channel", Channel.values)
def test_log_an_inquiry_from_every_channel(channel, client, manager):
    response = logged_in(client, manager).post(
        reverse("sales:new"),
        {
            "channel": channel,
            "received_at": "2026-11-09T09:30",
            "from_name": "Sam Lee",
            "from_email": "sam@example.com",
            "subject": "40 hoodies",
            "details": "Navy, logo on the back",
        },
    )
    inquiry = Inquiry.objects.get()
    assert response.status_code == 302
    assert response["Location"] == reverse("sales:detail", args=[inquiry.pk])
    assert inquiry.channel == channel and inquiry.status == Inquiry.Status.NEW
    assert AuditLog.objects.filter(entity_type="sales.inquiry", action="create").exists()


def test_open_list_hides_converted_and_declined(client, manager, customer):
    Inquiry.objects.create(channel=Channel.EMAIL, subject="open one")
    Inquiry.objects.create(channel=Channel.EMAIL, subject="gone", status=Inquiry.Status.DECLINED)
    page = logged_in(client, manager).get(reverse("sales:list")).content.decode()
    assert "open one" in page and "gone" not in page
    assert "gone" in client.get(reverse("sales:list") + "?show=all").content.decode()


def test_inquiry_page_suggests_customers_by_email(client, manager):
    Customer.objects.create(display_name="Sam Lee", email="sam@example.com")
    inquiry = Inquiry.objects.create(channel=Channel.EMAIL, from_email="SAM@example.com")
    page = logged_in(client, manager).get(reverse("sales:detail", args=[inquiry.pk]))
    assert "Possible Matches" in page.content.decode()
    assert "Sam Lee" in page.content.decode()


def test_create_customer_from_inquiry_then_convert_straight_to_invoice(client, manager):
    inquiry = Inquiry.objects.create(
        channel=Channel.ONLINE_STORE,
        from_name="Pat Doe",
        from_email="pat@example.com",
        subject="Store order 88",
    )
    c = logged_in(client, manager)
    c.post(reverse("sales:customer", args=[inquiry.pk]), {"customer": "new"})
    inquiry.refresh_from_db()
    assert inquiry.customer.display_name == "Pat Doe"
    assert inquiry.customer.email == "pat@example.com"
    assert inquiry.status == Inquiry.Status.IN_REVIEW

    response = c.post(
        reverse("sales:convert", args=[inquiry.pk]),
        {"start": Status.INVOICED, "title": "Store order 88", "square_number": "1042"},
    )
    job = Job.objects.get()
    assert response["Location"] == reverse("jobs:detail", args=[job.pk])
    assert (job.status, job.square_number, job.source) == ("invoiced", "1042", "online_store")
    inquiry.refresh_from_db()
    assert inquiry.status == Inquiry.Status.CONVERTED


def test_convert_without_customer_is_refused_with_a_message(client, manager):
    inquiry = Inquiry.objects.create(channel=Channel.PHONE, subject="Caps")
    response = logged_in(client, manager).post(
        reverse("sales:convert", args=[inquiry.pk]),
        {"start": Status.QUOTING, "title": "Caps"},
        follow=True,
    )
    assert "Pick or create the customer first" in response.content.decode()
    assert not Job.objects.exists()


def test_decline(client, manager):
    inquiry = Inquiry.objects.create(channel=Channel.PHONE, subject="Caps")
    logged_in(client, manager).post(reverse("sales:decline", args=[inquiry.pk]))
    inquiry.refresh_from_db()
    assert inquiry.status == Inquiry.Status.DECLINED


def test_avery_logs_inquiries_and_customers_but_cannot_convert(client, make_user, customer):
    c = logged_in(client, make_user("avery"))
    assert c.get(reverse("sales:new")).status_code == 200
    assert c.get(reverse("customers:new")).status_code == 200
    inquiry = Inquiry.objects.create(channel=Channel.WALK_IN, customer=customer, subject="x")
    page = c.get(reverse("sales:detail", args=[inquiry.pk])).content.decode()
    assert "Convert to job" not in page
    assert c.post(reverse("sales:convert", args=[inquiry.pk]), {}).status_code == 403


def test_production_cannot_open_inquiries_or_customers(client, make_user, customer):
    c = logged_in(client, make_user("production"))
    assert c.get(reverse("sales:list")).status_code == 403
    assert c.get(reverse("customers:list")).status_code == 403
    assert c.get(reverse("customers:detail", args=[customer.pk])).status_code == 403


def test_pages_need_sign_in(client):
    for name in ("sales:list", "customers:list", "jobs:list"):
        response = client.get(reverse(name))
        assert response.status_code == 302 and response["Location"].startswith("/login/")


# --- Customers --------------------------------------------------------------------


def _customer_post(**extra):
    data = {
        "c-display_name": "Acme Soccer Club",
        "c-company_name": "Acme Soccer Club",
        "c-email": "club@example.com",
        "c-phone": "403-555-0100",
        "c-notes": "",
        "contacts-TOTAL_FORMS": "1",
        "contacts-INITIAL_FORMS": "0",
        "contacts-MIN_NUM_FORMS": "0",
        "contacts-MAX_NUM_FORMS": "2",
        "contacts-0-full_name": "Coach Kim",
        "contacts-0-email": "kim@example.com",
        "contacts-0-is_primary": "on",
        "addresses-TOTAL_FORMS": "1",
        "addresses-INITIAL_FORMS": "0",
        "addresses-MIN_NUM_FORMS": "0",
        "addresses-MAX_NUM_FORMS": "1000",
        "addresses-0-kind": "shipping",
        "addresses-0-line1": "1 Main St",
        "addresses-0-city": "Calgary",
        "addresses-0-province": "AB",
        "addresses-0-postal_code": "T2P 1A1",
    }
    data.update(extra)
    return data


def test_create_customer_with_contact_and_address(client, manager):
    response = logged_in(client, manager).post(reverse("customers:new"), _customer_post())
    customer = Customer.objects.get()
    assert response["Location"] == reverse("customers:detail", args=[customer.pk])
    assert customer.contacts.get().full_name == "Coach Kim"
    assert customer.addresses.get().city == "Calgary"
    entity_types = set(AuditLog.objects.values_list("entity_type", flat=True))
    assert {"customers.customer", "customers.contact", "customers.address"} <= entity_types


def test_at_most_two_contacts(client, manager):
    data = _customer_post(
        **{
            "contacts-TOTAL_FORMS": "3",
            "contacts-1-full_name": "Second",
            "contacts-2-full_name": "Third",
        }
    )
    response = logged_in(client, manager).post(reverse("customers:new"), data)
    assert response.status_code == 200  # form shown again with the error
    assert not Contact.objects.exists()


def test_edit_customer_is_audited(client, manager, customer):
    data = _customer_post(**{"c-display_name": "Pat Doe-Smith", "contacts-TOTAL_FORMS": "0"})
    data["addresses-TOTAL_FORMS"] = "0"
    logged_in(client, manager).post(reverse("customers:edit", args=[customer.pk]), data)
    entry = AuditLog.objects.get(entity_type="customers.customer", action="update")
    assert entry.changes["display_name"] == ["Pat Doe", "Pat Doe-Smith"]
    assert entry.actor == manager


def test_customer_search(client, manager):
    Customer.objects.create(display_name="Acme Soccer Club")
    Customer.objects.create(display_name="Bow River Bakery")
    page = logged_in(client, manager).get(reverse("customers:list") + "?q=soccer")
    assert "Acme Soccer Club" in page.content.decode()
    assert "Bow River Bakery" not in page.content.decode()


def test_a_website_request_always_becomes_a_job_at_quoting(client, manager, customer):
    from apps.jobs import services

    inquiry = Inquiry.objects.create(channel=Channel.WEBSITE, customer=customer, subject="Hoodies")
    c = logged_in(client, manager)
    page = c.get(reverse("sales:detail", args=[inquiry.pk])).content.decode()
    assert "Quote first" in page and "Straight to invoice" not in page
    c.post(
        reverse("sales:convert", args=[inquiry.pk]),
        {"start": Status.QUOTING, "title": "50 hoodies"},
    )
    job = Job.objects.get()
    assert (job.status, job.source) == (Status.QUOTING, Channel.WEBSITE)

    other = Inquiry.objects.create(channel=Channel.WEBSITE, customer=customer, subject="Caps")
    job = services.convert_inquiry(other, user=manager, start=Status.INVOICED, title="Caps")
    assert job.status == Status.QUOTING  # enforced, not just hidden


def test_other_requests_can_still_go_straight_to_invoice(client, manager, customer):
    inquiry = Inquiry.objects.create(channel=Channel.PHONE, customer=customer, subject="Tees")
    page = logged_in(client, manager).get(reverse("sales:detail", args=[inquiry.pk]))
    assert "Straight to invoice" in page.content.decode()
