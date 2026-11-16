"""Step 2 week 2: the inbox, payment gates, customers, the nightly check and the health panel
(FR-04, FR-07, FR-08, INT-01, NFR-02)."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AppSetting
from apps.audit.models import AuditLog
from apps.core import health
from apps.customers.models import Customer
from apps.integrations import linking, nightly, square
from apps.integrations.models import SquareInvoice, WebhookEvent
from apps.jobs import gates, services
from apps.jobs.models import PaymentStatus, Status
from apps.jobs.services import InvalidMove

from .test_square_webhooks import event, invoice, run

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def square_on(settings):
    settings.SQUARE_ACCESS_TOKEN = ""
    settings.SQUARE_WEBHOOK_SIGNATURE_KEY = "k"
    settings.SQUARE_ENABLED = True
    settings.SQUARE_LOCATION_ID = "LOC1"


@pytest.fixture
def job(job_at):
    j = job_at(Status.MOCKUP_APPROVED)
    j.job_number = "G-1042"
    j.save()
    return j


def paid(job, status=PaymentStatus.PAID):
    job.payment_status = status
    job.save()
    return job


# --- Payment gates (FR-07) ----------------------------------------------------------


def test_the_default_gate_is_paid_in_full_before_materials_ordered():
    assert gates.configured() == {Status.MATERIALS_ORDERED: "paid"}


def test_an_unpaid_job_cant_reach_materials_ordered(job, manager):
    with pytest.raises(InvalidMove, match="Needs payment in full before Materials Ordered: Unpaid"):
        services.move(job, Status.MATERIALS_ORDERED, user=manager)


def test_a_deposit_isnt_enough_for_a_paid_gate(job, manager):
    with pytest.raises(InvalidMove):
        services.move(paid(job, PaymentStatus.DEPOSIT), Status.MATERIALS_ORDERED, user=manager)


def test_a_paid_job_moves(job, manager):
    job = services.move(paid(job), Status.MATERIALS_ORDERED, user=manager)
    assert job.status == Status.MATERIALS_ORDERED


def test_an_admin_overrides_with_a_reason_and_it_is_audited(job, admin_user):
    with pytest.raises(InvalidMove, match="reason"):
        services.move(job, Status.MATERIALS_ORDERED, user=admin_user)
    job = services.move(job, Status.MATERIALS_ORDERED, user=admin_user, note="Trusted school")
    assert job.status == Status.MATERIALS_ORDERED
    audit = AuditLog.objects.get(entity_type="jobs.job", action="override")
    assert audit.actor == admin_user
    assert audit.changes["payment_gate"][1].startswith("Needs payment in full")


def test_a_manager_cant_override_even_with_a_reason(job, manager):
    with pytest.raises(InvalidMove):
        services.move(job, Status.MATERIALS_ORDERED, user=manager, note="Please")


def test_skipping_ahead_doesnt_avoid_the_gate(job_at, manager):
    invoiced = job_at(Status.INVOICED)
    with pytest.raises(InvalidMove, match="Needs payment"):
        services.skip_mockup(invoiced, user=manager, note="Online store order")
    with pytest.raises(InvalidMove, match="Needs payment"):
        services.move(invoiced, Status.IN_PRODUCTION, user=manager, note="Rush")


def test_moving_back_is_never_gated(job_at, manager):
    job = job_at(Status.IN_PRODUCTION)
    job = services.move(job, Status.MOCKUP_IN_PROGRESS, user=manager, note="Redo")
    assert job.status == Status.MOCKUP_IN_PROGRESS


def test_no_gates_without_square(job, manager, settings):
    settings.SQUARE_ENABLED = False
    assert services.move(job, Status.MATERIALS_ORDERED, user=manager).status == (
        Status.MATERIALS_ORDERED
    )


def test_production_is_told_nothing_about_payment(job_at, make_user):
    AppSetting.objects.create(key=gates.SETTING, value={Status.SHIPPED: "paid"})
    avery = make_user("avery", "Avery")
    job = job_at(Status.READY)
    with pytest.raises(InvalidMove) as refused:
        services.move(job, Status.SHIPPED, user=avery)
    assert "ay" not in str(refused.value).replace("today", "")  # no "pay", "payment", "Unpaid"
    assert str(refused.value) == f"{gates.HIDDEN}."


def test_the_job_page_shows_the_gate(client, job, manager, admin_user):
    url = reverse("jobs:detail", args=[job.pk])
    client.force_login(manager)
    page = client.get(url).content.decode()
    assert "disabled" in page and "Needs payment in full before Materials Ordered" in page
    client.force_login(admin_user)
    page = client.get(url).content.decode()
    assert "Override and move" in page


def test_admin_sets_the_gates(client, admin_user, manager):
    url = reverse("integrations:payment_gates")
    client.force_login(manager)
    assert client.get(url).status_code == 403
    client.force_login(admin_user)
    assert client.get(url).status_code == 200
    client.post(url, {Status.MATERIALS_ORDERED: "deposit", Status.SHIPPED: "paid"})
    assert gates.configured() == {Status.MATERIALS_ORDERED: "deposit", Status.SHIPPED: "paid"}
    assert AuditLog.objects.filter(entity_type="accounts.appsetting", actor=admin_user).exists()


# --- The inbox (FR-04) --------------------------------------------------------------


@pytest.fixture
def unmatched(job):
    run(event(inv=invoice(title="Team tees")))
    return SquareInvoice.objects.get()


def test_an_unmatched_invoice_waits_in_the_inbox(client, manager, unmatched):
    client.force_login(manager)
    page = client.get(reverse("integrations:inbox")).content.decode()
    assert "Team tees" in page and "000123" in page
    assert "<h1>Invoices</h1>" in page and ">1</span>" in page  # the header count


def test_linking_by_hand_updates_the_job(client, manager, unmatched, job):
    job.status = Status.QUOTE_APPROVED
    job.save()
    client.force_login(manager)
    client.post(reverse("integrations:invoice_link", args=[unmatched.pk]), {"job_number": "G-1042"})
    unmatched.refresh_from_db()
    job.refresh_from_db()
    assert unmatched.job == job and unmatched.match_method == SquareInvoice.Match.MANUAL
    assert unmatched.linked_by == manager
    assert job.status == Status.INVOICED and job.amount_total_cents == 52500
    assert not linking.waiting().exists()


def test_an_unknown_job_number_is_refused(client, manager, unmatched):
    client.force_login(manager)
    page = client.post(
        reverse("integrations:invoice_link", args=[unmatched.pk]),
        {"job_number": "G-9"},
        follow=True,
    ).content.decode()
    assert "No single job" in page
    unmatched.refresh_from_db()
    assert unmatched.job is None


def test_unlink_puts_it_back_and_it_doesnt_relink_itself(client, manager, job):
    run(event())  # links itself by G-1042
    inv = SquareInvoice.objects.get()
    client.force_login(manager)
    client.post(reverse("integrations:invoice_unlink", args=[inv.pk]))
    inv.refresh_from_db()
    job.refresh_from_db()
    assert inv.job is None and job.amount_total_cents is None
    run(event("invoice.updated", "ev-2", invoice(version=2)))  # title still says G-1042
    inv.refresh_from_db()
    assert inv.job is None and linking.waiting().count() == 1


def test_not_a_job_leaves_the_inbox(client, manager, unmatched):
    client.force_login(manager)
    client.post(reverse("integrations:invoice_set_aside", args=[unmatched.pk]))
    unmatched.refresh_from_db()
    assert unmatched.not_a_job_by == manager and not linking.waiting().exists()


@pytest.mark.parametrize("role", ["production", "avery"])
def test_production_cant_use_the_inbox(client, make_user, unmatched, role):
    client.force_login(make_user(role, role.title()))
    assert client.get(reverse("integrations:inbox")).status_code == 403
    client.post(reverse("integrations:invoice_set_aside", args=[unmatched.pk]))
    unmatched.refresh_from_db()
    assert unmatched.not_a_job_at is None


def test_the_payment_panel(client, manager, job):
    run(event("invoice.payment_made", inv=invoice(status="PARTIALLY_PAID", paid=20000)))
    client.force_login(manager)
    page = client.get(reverse("jobs:detail", args=[job.pk]) + "?tab=details").content.decode()
    assert "$525.00" in page and "$200.00" in page and "$325.00" in page  # total, paid, balance
    assert "Unlink" in page and "Open in Square" in page


def test_home_shows_what_is_owed(client, manager, job):
    run(event("invoice.payment_made", inv=invoice(status="PARTIALLY_PAID", paid=20000)))
    client.force_login(manager)
    page = client.get(reverse("home")).content.decode()
    assert "$325.00" in page


# --- Customers (FR-08) --------------------------------------------------------------


def test_a_linked_invoice_gives_the_customer_its_square_id(job):
    run(event())
    job.customer.refresh_from_db()
    assert job.customer.square_customer_id == "CUST1"


def test_an_existing_square_link_is_never_changed(job):
    job.customer.square_customer_id = "OTHER"
    job.customer.save()
    run(event())
    job.customer.refresh_from_db()
    assert job.customer.square_customer_id == "OTHER"


def customer_event(email, square_id="SQ-9", event_id="c-1"):
    return {
        "type": "customer.created",
        "event_id": event_id,
        "data": {
            "type": "customer",
            "id": square_id,
            "object": {"customer": {"id": square_id, "email_address": email}},
        },
    }


def test_a_square_customer_with_a_matching_email_is_linked(customer):
    run(customer_event("PAT@example.com"))
    customer.refresh_from_db()
    assert customer.square_customer_id == "SQ-9"


def test_no_match_changes_nothing(customer):
    run(customer_event("someone@else.com"))
    customer.refresh_from_db()
    assert customer.square_customer_id == ""
    assert Customer.objects.count() == 1


# --- Setup problems and the health panel --------------------------------------------


def test_another_location_is_recorded_not_dropped(job):
    ev = run(event(inv=invoice(location="OTHER")))
    ev.refresh_from_db()
    assert ev.processed_at is not None and "location OTHER" in ev.last_error
    check = health.check_square_webhooks()
    assert check.status == health.WARN and "location OTHER" in check.detail


def test_a_refused_token_stops_retrying_and_shows_on_the_health_panel(job, settings, monkeypatch):
    settings.SQUARE_ACCESS_TOKEN = "token"

    def refuse(_id):
        raise square.SquareAccessError(
            "Square refused the access token: not authorized for this location"
        )

    monkeypatch.setattr(square, "get_invoice", refuse)
    ev = run(event())  # no exception: retrying can't fix it
    ev.refresh_from_db()
    assert ev.processed_at is None and "refused the access token" in ev.last_error
    WebhookEvent.objects.update(received_at=timezone.now() - timedelta(minutes=15))
    check = health.check_square_webhooks()
    assert check.status == health.FAIL and "not authorized" in check.detail


# --- The nightly check (INT-01) -----------------------------------------------------


@pytest.fixture
def api(settings, monkeypatch):
    """A fake Square: invoices by ID, the location's list, and its locations."""
    settings.SQUARE_ACCESS_TOKEN = "token"
    store = {"invoices": {}, "locations": ["LOC1"]}
    monkeypatch.setattr(square, "get_invoice", lambda i: store["invoices"].get(i))
    monkeypatch.setattr(square, "list_invoices", lambda loc, **kw: list(store["invoices"].values()))
    monkeypatch.setattr(square, "search_orders", lambda loc, **kw: iter(store.get("orders", [])))
    monkeypatch.setattr(square, "list_locations", lambda: store["locations"])
    monkeypatch.setattr(square, "get_order", lambda o: None)
    return store


def test_the_nightly_check_fixes_a_dropped_webhook(job, api):
    run(event(inv=invoice(title="G-1042 tees")))
    # The payment's webhook never arrived; Square has it.
    api["invoices"]["inv:0-1"] = invoice(version=2, status="PAID", paid=52500)
    result = nightly.run()
    job.refresh_from_db()
    assert job.payment_status == PaymentStatus.PAID
    assert result["fixed"] == 1 and result["error"] == ""
    assert health.check_square_nightly().status == health.OK


def test_the_nightly_check_finds_an_invoice_never_seen(job, api):
    fresh = invoice(id="inv:new", number="000200")
    fresh["updated_at"] = timezone.now().isoformat()
    api["invoices"]["inv:new"] = fresh
    nightly.run()
    assert SquareInvoice.objects.get(square_id="inv:new").job == job


def test_the_nightly_check_names_a_token_location_mismatch(api):
    api["locations"] = ["LPM"]
    result = nightly.run()
    assert "can't read location LOC1" in result["error"] and "LPM" in result["error"]
    assert health.check_square_nightly().status == health.FAIL


def test_old_webhook_bodies_are_deleted_after_90_days(api):
    WebhookEvent.objects.create(
        event_id="old", event_type="x", body={}, received_at=timezone.now() - timedelta(days=91)
    )
    WebhookEvent.objects.create(
        event_id="new", event_type="x", body={}, processed_at=timezone.now()
    )
    nightly.run()
    assert list(WebhookEvent.objects.values_list("event_id", flat=True)) == ["new"]


def test_the_nightly_check_retries_stuck_events(job, api):
    payload = event()
    WebhookEvent.objects.create(
        event_id="ev-1", event_type=payload["type"], square_object_id="inv:0-1", body=payload
    )
    api["invoices"]["inv:0-1"] = invoice()
    result = nightly.run()
    assert result["retried"] == 1
    assert WebhookEvent.objects.get().processed_at is not None


def test_the_customer_import(api, monkeypatch, customer):
    api["invoices"] = {
        "a": {
            **invoice(id="a"),
            "primary_recipient": {"customer_id": "SQ-PAT"},
            "created_at": timezone.now().isoformat(),
        },
        "b": {
            **invoice(id="b"),
            "primary_recipient": {"customer_id": "SQ-NEW"},
            "created_at": timezone.now().isoformat(),
        },
        "c": {
            **invoice(id="c"),
            "primary_recipient": {"customer_id": "SQ-OLD"},
            "created_at": "2019-01-01T00:00:00Z",
        },
    }
    people = {
        "SQ-PAT": {"id": "SQ-PAT", "email_address": "pat@example.com"},
        "SQ-NEW": {
            "id": "SQ-NEW",
            "given_name": "Sam",
            "family_name": "Lee",
            "email_address": "sam@x.com",
        },
    }
    monkeypatch.setattr(square, "get_customer", lambda cid: people.get(cid))
    dry = nightly.import_customers(dry_run=True)
    assert dry == {"square_customers": 2, "already_linked": 0, "matched": 1, "created": 1}
    assert Customer.objects.count() == 1
    nightly.import_customers()
    customer.refresh_from_db()
    assert customer.square_customer_id == "SQ-PAT"
    assert Customer.objects.get(square_customer_id="SQ-NEW").display_name == "Sam Lee"
    said = []
    again = nightly.import_customers(progress=said.append)
    assert again["already_linked"] == 2 and again["created"] == 0
    assert said and "2 customers to check" in said[0]  # the command prints these as it goes


def test_editing_an_app_setting_in_the_admin_is_audited(client, admin_user):
    """App settings are keyed by name, not number; the audit entry keeps the key."""
    admin_user.is_staff = True
    admin_user.save()
    AppSetting.objects.create(key="turnaround_days", value=14)
    client.force_login(admin_user)
    url = reverse("admin:accounts_appsetting_change", args=["turnaround_days"])
    response = client.post(url, {"key": "turnaround_days", "value": "10"})
    assert response.status_code == 302
    entry = AuditLog.objects.get(entity_type="accounts.appsetting")
    assert entry.changes["key"] == [None, "turnaround_days"]
