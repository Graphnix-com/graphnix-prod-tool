"""Production never sees money: no page or refresh fragment shows a price or payment
(NFR-02). Every URL in the project is visited, so a new page is covered without editing this.
"""

import re

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import URLResolver, get_resolver, reverse

from apps.integrations import square
from apps.jobs import files, money
from apps.jobs.models import Job, JobItem, JobItemSize, PaymentStatus, Status
from apps.production.models import DecorationMethod, ProductionTask
from apps.sales.models import Inquiry

from .test_square_webhooks import invoice

pytestmark = pytest.mark.django_db

# Payment states, amounts in the forms the job holds them, and the words around them.
MONEY = re.compile(r"Deposit|Unpaid|Refunded|Payment|payment|\$|1,234\.56|1234\.56|123456|500\.00")
# Extra query strings that change what a page shows.
VARIANTS = {
    "jobs:list": ["", "?all=1", "?all=1&partial=1", "?side=1&all=1", "?payment=deposit&all=1"],
    "jobs:detail": ["?tab=items", "?tab=timeline", "?tab=details"],
    "jobs:search": ["?q=Acme", "?q=5521"],
}


def every_url_name():
    def walk(patterns, namespace=None):
        for p in patterns:
            if isinstance(p, URLResolver):
                if str(p.pattern).startswith("admin"):
                    continue
                yield from walk(p.url_patterns, p.namespace or namespace)
            elif p.name:
                yield f"{namespace}:{p.name}" if namespace else p.name, list(p.pattern.converters)

    return list(walk(get_resolver().url_patterns))


@pytest.fixture
def world(customer, make_user, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.SQUARE_ENABLED = True  # payment gates on
    settings.SQUARE_ACCESS_TOKEN = ""
    settings.SQUARE_LOCATION_ID = ""
    job = Job.objects.create(
        customer=customer,
        title="Acme hoodies",
        status=Status.INVOICED,
        square_number="5521",
        payment_status=PaymentStatus.DEPOSIT,
        amount_total_cents=123456,
        amount_paid_cents=50000,
    )
    item = JobItem.objects.create(job=job, description="Gildan 18500", is_sized=True)
    JobItemSize.objects.create(job_item=item, size="M", quantity=12)
    task = ProductionTask.objects.create(
        job=job, job_item=item, decoration_method=DecorationMethod.objects.get(code="dtf")
    )
    inquiry = Inquiry.objects.create(
        customer=customer, channel="email", subject="Hoodies", details="50 hoodies"
    )
    # Step 2: a linked invoice, paid then partly refunded (timeline entries from Square),
    # and one waiting in the inbox.
    linked = invoice(id="inv:a", title=f"{job.job_number} hoodies", total=123456, paid=50000)
    square.apply({**linked, "status": "PARTIALLY_PAID"}, event_id="e1")
    square.apply({**linked, "version": 2, "status": "PARTIALLY_REFUNDED"}, refunded=1000)
    waiting = square.apply(invoice(id="inv:b", number="777", title="Counter sale"))
    upload = SimpleUploadedFile("mockup.png", b"\x89PNG fake", content_type="image/png")
    job_file = files.add(job, [upload], user=make_user("manager", "Office"))[0]
    return {
        "pk": {
            "customers": customer.pk,
            "sales": inquiry.pk,
            "jobs": job.pk,
            "integrations": waiting.pk,
            None: job.pk,
        },
        "item_pk": item.pk,
        "file_pk": job_file.pk,
        "task_pk": task.pk,
        "job": job,
    }


def urls(world):
    for name, params in every_url_name():
        namespace = name.split(":")[0] if ":" in name else None
        kwargs = {}
        for param in params:
            if param == "pk":
                kwargs["pk"] = world["pk"].get(namespace, world["job"].pk)
            elif param in world:
                kwargs[param] = world[param]
            else:
                raise AssertionError(f"Teach this test the URL parameter {param!r} ({name})")
        base = reverse(name, kwargs=kwargs)
        for variant in VARIANTS.get(name, [""]):
            yield name, base + variant


@pytest.mark.parametrize("role", ["production", "avery"])
def test_no_page_shows_money_to_production(role, client, make_user, world):
    client.force_login(make_user(role, role.title()))
    seen = 0
    for name, url in urls(world):
        response = client.get(url)
        if response.status_code != 200 or response.streaming:
            continue  # a downloaded file is what staff uploaded, not a page
        body = response.content.decode()
        found = MONEY.search(body)
        assert not found, f"{role} sees {found.group()!r} on {url} ({name})"
        seen += 1
    assert seen >= 6  # board, fragments, job tabs, search: the test really looked


def test_the_markers_are_real_admin_and_manager_see_payment(client, manager, world):
    client.force_login(manager)
    page = client.get(reverse("jobs:detail", args=[world["job"].pk]) + "?tab=details")
    assert MONEY.search(page.content.decode())


# --- Prices typed into text Production reads -------------------------------------


@pytest.mark.parametrize(
    "text",
    ["$450", "450$", "deposit of $ 200", "450 dollars", "CAD 300", "300 cad", "$1,200.00"],
)
def test_amounts_are_spotted(text):
    assert money.has_amount(text)


@pytest.mark.parametrize(
    "text", ["50 tees, left chest", "3XL x 2", "PMS 186 red", "Order 1042", "", None]
)
def test_ordinary_notes_pass(text):
    assert not money.has_amount(text)


def test_a_note_with_a_price_is_refused(client, manager, world):
    client.force_login(manager)
    job = world["job"]
    page = client.post(
        reverse("jobs:note_add", args=[job.pk]), {"body": "Paid $500 deposit"}, follow=True
    )
    assert "Leave prices out" in page.content.decode()
    assert not job.notes.exists()


def test_a_move_reason_with_a_price_is_refused(client, manager, world):
    client.force_login(manager)
    job = world["job"]
    client.post(
        reverse("jobs:action", args=[job.pk]),
        {"action": "move", "to_status": Status.QUOTING, "note": "Re-quote at $900"},
    )
    job.refresh_from_db()
    assert job.status == Status.INVOICED


def test_a_title_with_a_price_is_refused(client, manager, world):
    client.force_login(manager)
    job = world["job"]
    data = {"title": "Hoodies $1200", "priority": 2}
    page = client.post(reverse("jobs:edit", args=[job.pk]), data)
    assert "Leave prices out" in page.content.decode()
