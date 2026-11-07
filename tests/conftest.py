import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from apps.accounts import roles
from apps.customers.models import Customer
from apps.jobs.models import Job

User = get_user_model()

# The people in the spec's role table, by what they can do.
ROLE_GROUPS = {
    "admin": [roles.ADMIN],
    "manager": [roles.MANAGER],
    "production": [roles.PRODUCTION],
    "avery": [roles.PRODUCTION, roles.QA_AND_SHIPPING, roles.CUSTOMER_ENTRY],
    "customer_entry": [roles.CUSTOMER_ENTRY],
}


@pytest.fixture
def make_user(db):
    def make(role: str, name: str | None = None):
        user = User.objects.create_user(email=None, full_name=name or role.title())
        user.groups.add(*Group.objects.filter(name__in=ROLE_GROUPS[role]))
        return User.objects.get(pk=user.pk)  # fresh, so the permission cache is empty

    return make


@pytest.fixture
def admin_user(make_user):
    return make_user("admin", "Jenn")


@pytest.fixture
def manager(make_user):
    return make_user("manager", "McKenna")


@pytest.fixture
def customer(db):
    return Customer.objects.create(display_name="Pat Doe", email="pat@example.com")


@pytest.fixture
def job_at(customer):
    """A job placed directly at a stage, for testing moves out of it."""

    def make(status: str, **fields):
        return Job.objects.create(customer=customer, title="50 tees", status=status, **fields)

    return make
