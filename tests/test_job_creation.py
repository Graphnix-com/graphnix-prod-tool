"""Creating jobs and converting inquiries (FR-01, FR-02)."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection

from apps.audit.models import AuditLog
from apps.customers.models import Customer
from apps.jobs import services
from apps.jobs.models import Job, Status
from apps.sales.models import Channel, Inquiry

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("start", [Status.QUOTING, Status.INVOICED])
def test_create_job_at_either_start(start, manager, customer):
    job = services.create_job(customer=customer, title="Team hoodies", start=start, user=manager)
    assert job.status == start
    assert job.job_number.startswith("G-")
    first = job.history.get()
    assert (first.from_status, first.to_status, first.changed_by) == ("", start, manager)
    assert AuditLog.objects.filter(entity_id=job.pk, action="create").count() == 1


def test_create_job_refuses_other_starts(manager, customer):
    with pytest.raises(services.InvalidMove):
        services.create_job(customer=customer, title="x", start=Status.MOCKUP_SENT, user=manager)


def test_production_and_customer_entry_cannot_create_jobs(make_user, customer):
    for role in ("production", "avery", "customer_entry"):
        with pytest.raises(PermissionDenied):
            services.create_job(
                customer=customer, title="x", start=Status.QUOTING, user=make_user(role)
            )


def test_duplicate_square_number_is_refused(manager, customer):
    services.create_job(
        customer=customer, title="a", start=Status.INVOICED, user=manager, square_number="1042"
    )
    with pytest.raises(ValidationError):
        services.create_job(
            customer=customer, title="b", start=Status.QUOTING, user=manager, square_number=" 1042 "
        )
    # The database refuses it too, whatever the code path.
    with pytest.raises(IntegrityError):
        Job.objects.create(customer=customer, title="c", square_number="1042")


def test_many_jobs_without_a_square_number_yet(manager, customer):
    for n in range(3):
        services.create_job(customer=customer, title=f"job {n}", start=Status.QUOTING, user=manager)
    assert Job.objects.filter(square_number="").count() == 3


def test_display_number_prefers_square(customer):
    job = Job.objects.create(customer=customer, title="x")
    assert job.display_number == job.job_number
    job.square_number = "1042"
    assert job.display_number == "1042"


@pytest.mark.django_db(transaction=True)
def test_job_numbers_stay_unique_under_concurrent_creates():
    """20 conversions at once, on separate database connections, get 20 distinct numbers."""
    user = get_user_model().objects.create_superuser("boss@graphnix.com", "a-long-password-1")
    customer = Customer.objects.create(display_name="Busy Customer")

    def create(n: int) -> str:
        try:
            job = services.create_job(
                customer=customer, title=f"order {n}", start=Status.QUOTING, user=user
            )
            return job.job_number
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=10) as pool:
        numbers = list(pool.map(create, range(20)))
    assert len(set(numbers)) == 20
    assert Job.objects.count() == 20


# --- Converting an inquiry -------------------------------------------------------


def test_convert_inquiry_carries_channel_and_marks_it_converted(manager, customer):
    inquiry = Inquiry.objects.create(
        channel=Channel.ONLINE_STORE, customer=customer, subject="Store order #88"
    )
    job = services.convert_inquiry(inquiry, user=manager, start=Status.INVOICED)
    inquiry.refresh_from_db()
    assert inquiry.status == Inquiry.Status.CONVERTED
    assert job.inquiry == inquiry
    assert job.source == Channel.ONLINE_STORE
    assert job.title == "Store order #88"
    assert job.status == Status.INVOICED


def test_convert_needs_a_customer_and_happens_once(manager, customer):
    inquiry = Inquiry.objects.create(channel=Channel.PHONE, from_name="Sam", subject="Caps")
    with pytest.raises(ValidationError):
        services.convert_inquiry(inquiry, user=manager, start=Status.QUOTING)
    services.convert_inquiry(inquiry, user=manager, start=Status.QUOTING, customer=customer)
    with pytest.raises(services.InvalidMove):
        services.convert_inquiry(inquiry, user=manager, start=Status.QUOTING, customer=customer)


@pytest.mark.parametrize("channel", Channel.values)
def test_one_inquiry_per_channel(channel, customer):
    inquiry = Inquiry.objects.create(channel=channel, customer=customer)
    assert inquiry.status == Inquiry.Status.NEW
    assert str(inquiry).startswith(inquiry.get_channel_display())
