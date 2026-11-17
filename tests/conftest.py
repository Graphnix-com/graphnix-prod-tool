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


@pytest.fixture(autouse=True)
def _plain_static_files(settings):
    """With DEBUG off (as in CI), settings use the manifest storage, which needs
    `collectstatic` first. Tests render pages without it."""
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
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


def mockup_version(job, name="mockup.png"):
    """A mock-up version on the job, its file already stored (tests don't need the bytes)."""
    from django.db.models import Max

    from apps.jobs.models import JobFile, MockupVersion

    f = JobFile.objects.create(job=job, file=f"job_files/{job.pk}/{name}", name=name, size=1)
    last = job.mockups.aggregate(n=Max("version"))["n"] or 0
    return MockupVersion.objects.create(job=job, version=last + 1, file=f)


def approval_for(job, approved_by="Pat Doe"):
    """What staff give to move a job to Mock-up approved: a version, a name, every tick."""
    from apps.jobs import mockups

    version = job.mockups.first() or mockup_version(job)
    return {
        "mockup": version.pk,
        "approval": {"approved_by": approved_by, "ticked": mockups.checks()},
    }
