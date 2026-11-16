"""The Customers list: one row per name; same-name records share an email dropdown."""

import pytest
from django.urls import reverse

from apps.customers.models import Customer
from apps.customers.views import by_name

pytestmark = pytest.mark.django_db


@pytest.fixture
def sams():
    return [
        Customer.objects.create(
            display_name="Sam Lee", email="zed@example.com", phone="403-555-0101"
        ),
        Customer.objects.create(display_name="sam  lee", email="Amy@example.com"),
        Customer.objects.create(display_name="Sam Lee", email=""),
        Customer.objects.create(display_name="Sam Lee", email="mid@example.com"),
    ]


def page(client, user, q=""):
    client.force_login(user)
    return client.get(reverse("customers:list"), {"q": q} if q else {}).content.decode()


def test_same_name_is_one_group_sorted_by_email(sams, customer):
    groups = by_name(Customer.objects.all())
    assert [len(g["customers"]) for g in groups] == [1, 4]  # Pat Doe, then the Sams
    emails = [c.email for c in groups[1]["customers"]]
    assert emails == ["Amy@example.com", "mid@example.com", "zed@example.com", ""]


def test_the_name_shown_is_the_usual_spelling(sams):
    assert by_name(Customer.objects.all())[0]["name"] == "Sam Lee"


def test_the_list_shows_the_name_once_with_an_email_dropdown(client, manager, sams, customer):
    html = page(client, manager)
    row = html.split('class="same-name"')[1].split("</tr>")[0]
    assert "4 Emails" in row and html.count("Sam Lee") == 1
    order = [
        row.index(e) for e in ("Amy@example.com", "mid@example.com", "zed@example.com", "No email")
    ]
    assert order == sorted(order)
    for c in sams:
        assert reverse("customers:detail", args=[c.pk]) in row
    assert reverse("customers:detail", args=[customer.pk]) in html  # Pat Doe: a normal row
    assert "pat@example.com" in html


def test_search_groups_the_matches(client, manager, sams):
    html = page(client, manager, "example.com")
    assert "3 Emails" in html and "No email" not in html


def test_different_companies_are_listed_per_email(client, manager):
    Customer.objects.create(display_name="Alex Kim", email="a@one.com", company_name="One Ltd")
    Customer.objects.create(display_name="Alex Kim", email="a@two.com", company_name="Two Ltd")
    row = page(client, manager).split('class="same-name"')[1].split("</tr>")[0]
    assert "One Ltd" in row and "Two Ltd" in row


def test_without_contact_details_no_emails_show(client, make_user, sams):
    from django.contrib.auth.models import Permission

    user = make_user("production")
    user.user_permissions.add(Permission.objects.get(codename="view_customer"))
    user = type(user).objects.get(pk=user.pk)
    html = page(client, user)
    assert "4 Records" in html and "example.com" not in html


def test_the_limit_counts_names_not_records(client, manager, monkeypatch, sams):
    from apps.customers import views

    monkeypatch.setattr(views, "LIST_LIMIT", 1)
    Customer.objects.create(display_name="Zoe Park", email="zoe@example.com")
    html = page(client, manager)
    assert "4 Emails" in html and "Zoe Park" not in html and "Showing the first" in html
