"""The board, its filters, the Home page and header search (FR-31, FR-33, FR-34)."""

from datetime import date, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.customers.models import Customer
from apps.jobs import board
from apps.jobs.flags import Flagger
from apps.jobs.models import HoldReason, Job, PaymentStatus, Status
from apps.production.models import DecorationMethod, ProductionTask

pytestmark = pytest.mark.django_db
TODAY = date(2026, 11, 12)  # a Thursday


def cards(lanes) -> dict[str, list[str]]:
    """{status: [job titles]} from board.board()."""
    return {s: [c.job.title for c in cs] for _, columns in lanes for s, _, cs in columns}


def run(**params):
    return cards(
        board.board(
            board.Filters.from_request(params, can_see_money=True),
            show_side=params.get("side") == "1",
            flagger=Flagger(on=TODAY),
        )
    )


def make(customer, status, title, **fields):
    return Job.objects.create(customer=customer, status=status, title=title, **fields)


def add_task(job, method_name, **fields):
    return ProductionTask.objects.create(
        job=job, decoration_method=DecorationMethod.objects.get(name=method_name), **fields
    )


def test_every_stage_has_its_column_and_closed_jobs_leave(customer):
    for status in Status.values:
        make(customer, status, f"job {status}")
    shown = run()
    for status in board.LANES[0][1] + board.LANES[3][1]:
        assert shown[status] == [f"job {status}"]
    assert Status.CLOSED not in shown
    assert Status.ON_HOLD not in shown and Status.CANCELLED not in shown


def test_side_states_on_request_and_old_cancellations_drop_off(customer):
    make(customer, Status.ON_HOLD, "held")
    recent = make(customer, Status.CANCELLED, "cancelled last week")
    old = make(customer, Status.CANCELLED, "cancelled in spring")
    Job.objects.filter(pk=old.pk).update(status_changed_at=timezone.now() - timedelta(days=90))
    Job.objects.filter(pk=recent.pk).update(status_changed_at=timezone.now())
    shown = cards(
        board.board(board.Filters(), show_side=True, flagger=Flagger(on=timezone.localdate()))
    )
    assert shown[Status.ON_HOLD] == ["held"]
    assert shown[Status.CANCELLED] == ["cancelled last week"]


def test_column_order_sooner_first_then_due_date_then_no_date(customer):
    make(customer, Status.QUOTING, "no date")
    make(customer, Status.QUOTING, "due later", due_date=TODAY + timedelta(days=20))
    make(customer, Status.QUOTING, "due sooner", due_date=TODAY + timedelta(days=10))
    make(customer, Status.QUOTING, "rush", priority=1, due_date=TODAY + timedelta(days=30))
    assert run()[Status.QUOTING] == ["rush", "due sooner", "due later", "no date"]


def test_filter_by_method(customer):
    printed = make(customer, Status.MOCKUP_SENT, "printed")
    stitched = make(customer, Status.MOCKUP_SENT, "stitched")
    add_task(printed, "Screen print")
    add_task(printed, "Screen print", placement="back")  # two tasks: still one card
    add_task(stitched, "Embroidery")
    method = DecorationMethod.objects.get(name="Screen print")
    assert run(method=str(method.pk))[Status.MOCKUP_SENT] == ["printed"]


def test_filter_by_due(customer):
    make(customer, Status.QUOTING, "overdue", due_date=TODAY - timedelta(days=1))
    make(customer, Status.QUOTING, "at risk", due_date=date(2026, 11, 18))
    # Already in production, so due soon but not "at risk".
    make(customer, Status.IN_PRODUCTION, "in 6 days", due_date=TODAY + timedelta(days=6))
    make(customer, Status.QUOTING, "next month", due_date=TODAY + timedelta(days=40))
    make(customer, Status.QUOTING, "undated")
    assert run(due="overdue")[Status.QUOTING] == ["overdue"]
    at_risk = run(due="at_risk")
    assert at_risk[Status.QUOTING] == ["at risk"] and at_risk[Status.IN_PRODUCTION] == []
    this_week = run(due="this_week")
    assert this_week[Status.QUOTING] == ["at risk"]
    assert this_week[Status.IN_PRODUCTION] == ["in 6 days"]
    assert run(due="none")[Status.QUOTING] == ["undated"]


def test_filter_by_assignee(customer, make_user):
    rommel = make_user("production", "Rommel")
    mine = make(customer, Status.IN_PRODUCTION, "Rommel's")
    make(customer, Status.IN_PRODUCTION, "unassigned")
    add_task(mine, "Embroidery", assigned_to=rommel)
    assert run(assignee=str(rommel.pk))[Status.IN_PRODUCTION] == ["Rommel's"]


def test_filter_by_customer_name_or_company(customer):
    club = Customer.objects.create(display_name="Coach Kim", company_name="Acme Soccer Club")
    make(club, Status.QUOTING, "kit")
    make(customer, Status.QUOTING, "Pat's")
    assert run(customer="soccer")[Status.QUOTING] == ["kit"]
    assert run(customer="pat")[Status.QUOTING] == ["Pat's"]


def test_filter_by_payment_status_only_for_money_roles(customer):
    make(customer, Status.INVOICED, "deposit paid", payment_status=PaymentStatus.DEPOSIT)
    make(customer, Status.INVOICED, "unpaid")
    assert run(payment="deposit")[Status.INVOICED] == ["deposit paid"]
    ignored = board.Filters.from_request({"payment": "deposit"}, can_see_money=False)
    assert ignored.payment == ""


def test_board_page_for_production_has_no_payment_filter(client, make_user, customer):
    make(customer, Status.INVOICED, "deposit paid", payment_status=PaymentStatus.DEPOSIT)
    client.force_login(make_user("production"))
    page = client.get(reverse("jobs:list") + "?all=1").content.decode()
    assert "deposit paid" in page
    assert 'name="payment"' not in page and "Deposit" not in page


def test_board_page_filters_and_refresh_fragment(client, manager, customer):
    make(customer, Status.QUOTING, "Pat hoodies")
    client.force_login(manager)
    page = client.get(reverse("jobs:list") + "?customer=pat").content.decode()
    assert "Pat hoodies" in page and 'name="payment"' in page
    fragment = client.get(reverse("jobs:list") + "?partial=1").content.decode()
    assert "Pat hoodies" in fragment and "<html" not in fragment


def test_cards_show_flags_and_methods(client, manager, customer):
    job = make(customer, Status.QUOTING, "late", due_date=timezone.localdate() - timedelta(days=2))
    add_task(job, "Embroidery")
    client.force_login(manager)
    page = client.get(reverse("jobs:list")).content.decode()
    assert "Overdue" in page and "Embroidery" in page


# --- Home page --------------------------------------------------------------------


def panel(panels, title):
    p = next(p for p in panels if p.title == title)
    return [c.job.title for c in p.graphnix], [c.job.title for c in p.customer]


def test_home_splits_by_who_acts_next(customer):
    make(customer, Status.QUOTING, "to quote")
    make(customer, Status.QUOTE_SENT, "quote out")
    make(customer, Status.MOCKUP_SENT, "proof out")
    make(
        customer,
        Status.ON_HOLD,
        "changing proof",
        resume_status=Status.MOCKUP_SENT,
        hold_reason_code=HoldReason.MOCKUP_CHANGE,
    )
    make(
        customer,
        Status.ON_HOLD,
        "blanks late",
        resume_status=Status.MATERIALS_ORDERED,
        hold_reason_code=HoldReason.PRODUCT_DELAY,
    )
    make(customer, Status.INVOICED, "invoiced")
    panels = board.home(Flagger(on=TODAY))
    assert panel(panels, "Quotes") == (["to quote"], ["quote out"])
    assert panel(panels, "Mock-ups") == ([], ["proof out", "changing proof"])
    assert panel(panels, "Invoices") == (["invoiced"], [])  # the mock-up to draw
    every_title = [c.job.title for p in panels for c in p.graphnix + p.customer]
    assert "blanks late" not in every_title


def test_home_almost_due_lists_overdue_and_at_risk_soonest_first(customer):
    make(customer, Status.MATERIALS_ORDERED, "at risk", due_date=date(2026, 11, 18))
    make(customer, Status.QUOTE_SENT, "overdue", due_date=date(2026, 11, 10))
    make(customer, Status.IN_PRODUCTION, "in production", due_date=date(2026, 11, 18))
    make(customer, Status.QUOTING, "plenty of time", due_date=date(2026, 12, 20))
    assert panel(board.home(Flagger(on=TODAY)), "Almost Due") == (["at risk"], ["overdue"])


def test_home_page_renders(client, manager, customer):
    make(customer, Status.QUOTE_SENT, "quote out")
    client.force_login(manager)
    page = client.get("/").content.decode()
    assert "Waiting On Customer" in page and "quote out" in page


# --- Search -----------------------------------------------------------------------


def test_search_by_square_number_goes_straight_to_the_job(client, manager, customer):
    job = make(customer, Status.QUOTING, "x", square_number="5521")
    client.force_login(manager)
    response = client.get(reverse("jobs:search") + "?q=5521")
    assert response["Location"] == reverse("jobs:detail", args=[job.pk])


@pytest.mark.parametrize("query", ["G-1000", "g-1000", "1000"])
def test_search_by_internal_number(query, client, manager, customer):
    job = make(customer, Status.QUOTING, "x")
    Job.objects.filter(pk=job.pk).update(job_number="G-1000")
    client.force_login(manager)
    assert client.get(reverse("jobs:search") + f"?q={query}")["Location"].endswith(
        f"/jobs/{job.pk}/"
    )


def test_a_number_matching_two_jobs_lists_both(client, manager, customer):
    a = make(customer, Status.QUOTING, "Square 1042", square_number="1042")
    b = make(customer, Status.QUOTING, "internal 1042")
    Job.objects.filter(pk=b.pk).update(job_number="G-1042")
    client.force_login(manager)
    page = client.get(reverse("jobs:search") + "?q=1042").content.decode()
    assert a.title in page and b.title in page


def test_search_by_customer_name_and_company(client, manager):
    club = Customer.objects.create(display_name="Coach Kim", company_name="Acme Soccer Club")
    make(club, Status.QUOTING, "Team kit")
    client.force_login(manager)
    for q in ("kim", "acme"):
        assert "Team kit" in client.get(reverse("jobs:search") + f"?q={q}").content.decode()


def test_search_by_email_only_for_people_who_see_contacts(client, manager, make_user):
    club = Customer.objects.create(display_name="Coach Kim", email="kim@acme.example")
    make(club, Status.QUOTING, "Team kit")
    client.force_login(manager)
    assert "Team kit" in client.get(reverse("jobs:search") + "?q=acme.example").content.decode()
    client.force_login(make_user("production"))
    page = client.get(reverse("jobs:search") + "?q=acme.example").content.decode()
    assert "Team kit" not in page


def test_search_finds_closed_jobs_too(client, manager, customer):
    make(customer, Status.CLOSED, "Last year's order")
    client.force_login(manager)
    page = client.get(reverse("jobs:search") + "?q=last year").content.decode()
    assert "Last year&#x27;s order" in page


# --- Assigned to me -----------------------------------------------------------------


def test_assigned_to_me_lists_my_jobs_across_stages_soonest_first(customer, make_user):
    rommel = make_user("production", "Rommel")
    isaac = make_user("production", "Isaac")
    later = make(customer, Status.QUOTING, "later", due_date=TODAY + timedelta(days=20))
    undated = make(customer, Status.IN_PRODUCTION, "undated")
    soon = make(customer, Status.QA, "soon", due_date=TODAY + timedelta(days=2))
    held = make(customer, Status.ON_HOLD, "held", due_date=TODAY + timedelta(days=5))
    closed = make(customer, Status.CLOSED, "closed", due_date=TODAY)
    cancelled = make(customer, Status.CANCELLED, "cancelled", due_date=TODAY)
    theirs = make(customer, Status.QUOTING, "Isaac's", due_date=TODAY)
    for job in (later, undated, soon, held, closed, cancelled):
        add_task(job, "Screen print", assigned_to=rommel, placement="front")
    add_task(soon, "Embroidery", assigned_to=rommel)  # two of mine: still one card
    add_task(soon, "Heat press", assigned_to=isaac)
    add_task(theirs, "Embroidery", assigned_to=isaac)

    mine = board.assigned_to(rommel, Flagger(on=TODAY))
    assert [c.job.title for c in mine] == ["soon", "held", "later", "undated"]
    assert mine[0].mine == ["Screen print · front", "Embroidery"]


def test_assigned_to_me_ignores_the_board_filters(client, customer, make_user):
    rommel = make_user("production", "Rommel")
    add_task(make(customer, Status.QUOTING, "Rommel's kit"), "Screen print", assigned_to=rommel)
    client.force_login(rommel)
    page = client.get(reverse("jobs:list") + "?customer=nobody-matches").content.decode()
    mine = page.split('aria-label="Assigned to me"')[1].split("</aside>")[0]
    assert "Rommel&#x27;s kit" in mine and "Quoting" in mine and "Assigned: Screen print" in mine


def test_assigned_to_me_is_in_the_refresh_and_empty_says_so(client, manager, customer):
    client.force_login(manager)
    fragment = client.get(reverse("jobs:list") + "?partial=1").content.decode()
    assert "Assigned to me" in fragment and "Nothing assigned." in fragment


def test_a_busy_column_shows_three_then_see_all(client, manager, customer):
    for n in range(5):
        make(customer, Status.QUOTING, f"Busy column job {n}")
    make(customer, Status.QUOTE_SENT, "Quiet column job")
    client.force_login(manager)
    html = client.get(reverse("jobs:list")).content.decode()
    assert html.count("See all 5 jobs") == 1  # only the busy column
    shown, rest = html.split("See all 5 jobs", 1)
    assert sum(f"Busy column job {n}" in shown for n in range(5)) == 3
    assert sum(f"Busy column job {n}" in rest for n in range(5)) == 2


def test_home_lists_show_three_then_see_all(client, manager, customer):
    for n in range(4):
        make(customer, Status.QUOTING, f"Waiting quote {n}")
    client.force_login(manager)
    html = client.get(reverse("home")).content.decode()
    assert "See all 4 jobs" in html
    shown = html.split("See all 4 jobs", 1)[0]
    assert sum(f"Waiting quote {n}" in shown for n in range(4)) == 3


def test_purchasing_is_not_in_the_header(client, manager):
    client.force_login(manager)
    nav = client.get(reverse("jobs:list")).content.decode().split("</nav>", 1)[0]
    assert "Purchasing" not in nav and "Board" in nav
