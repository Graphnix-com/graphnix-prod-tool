"""Every admin list and a job's page open without errors, and admin edits are audited."""

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.customers.models import Customer
from apps.jobs.models import Job

pytestmark = pytest.mark.django_db


@pytest.fixture
def boss_client(client):
    boss = get_user_model().objects.create_superuser("boss@graphnix.com", "a-long-password-1")
    client.force_login(boss)
    return client


def test_every_admin_list_opens(boss_client):
    for model in admin.site._registry:
        url = reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist")
        assert boss_client.get(url).status_code == 200, url


def test_job_page_opens_and_jobs_cannot_be_added_in_admin(boss_client, customer):
    job = Job.objects.create(customer=customer, title="Tees")
    assert boss_client.get(reverse("admin:jobs_job_change", args=[job.pk])).status_code == 200
    assert boss_client.get(reverse("admin:jobs_job_add")).status_code == 403


def test_customer_edit_in_admin_is_audited(boss_client):
    customer = Customer.objects.create(display_name="Old Name")
    url = reverse("admin:customers_customer_change", args=[customer.pk])
    form = {
        "display_name": "New Name",
        "company_name": "",
        "email": "",
        "phone": "",
        "square_customer_id": "",
        "notes": "",
        "contacts-TOTAL_FORMS": "1",
        "contacts-INITIAL_FORMS": "0",
        "contacts-0-full_name": "Sam Lee",
        "contacts-0-receives_notifications": "on",
        "addresses-TOTAL_FORMS": "0",
        "addresses-INITIAL_FORMS": "0",
    }
    response = boss_client.post(url, form)
    assert response.status_code == 302, response.content[:2000]
    entry = AuditLog.objects.get(entity_type="customers.customer", action="update")
    assert entry.changes["display_name"] == ["Old Name", "New Name"]
    assert AuditLog.objects.filter(entity_type="customers.contact", action="create").exists()
