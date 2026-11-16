"""The website's contact form emails become requests."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core import health
from apps.customers.models import Customer
from apps.m365 import graph
from apps.sales import webform
from apps.sales.models import Channel, Inquiry

pytestmark = pytest.mark.django_db

SUBJECT = "New contact form message for Graphnix.com via Contact Us"
SENDER = "notifications@mail.conversations.godaddy.com"
BODY = """Graphnix.com received a new message.

Name
Pat Doe

Email
pat@example.com

Phone
4035550100

Expected completion date?

Describe your project
100 shirts
Two colours, front and back

Device
desktop

Language
en-CA

Submitted_from
Contact us

Open Conversations

Want to forward this message to your team?
Invite them to join your Conversations inbox instead
"""


def message(id="m1", subject=SUBJECT, body=BODY, received=None, sender=SENDER):
    return {
        "id": id,
        "subject": subject,
        "from": {"emailAddress": {"name": "Pat Doe", "address": sender}},
        "receivedDateTime": (received or timezone.now()).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "body": {"contentType": "text", "content": body},
    }


# --- Reading the email -----------------------------------------------------------------


def test_the_answers_are_read():
    found = webform.parse(SUBJECT, BODY)
    assert found["form"] == "Contact Us"
    assert (found["name"], found["email"], found["phone"]) == (
        "Pat Doe",
        "pat@example.com",
        "4035550100",
    )
    assert found["due"] == "" and found["project"] == "100 shirts\nTwo colours, front and back"
    assert found["other"] == []  # device, language: not kept


def test_answers_on_one_line_are_read_too():
    body = "Name: Pat Doe\nEmail: pat@example.com\nExpected completion date?: Dec 5\n"
    found = webform.parse(SUBJECT, body)
    assert (found["name"], found["email"], found["due"]) == ("Pat Doe", "pat@example.com", "Dec 5")


def test_a_question_the_form_gains_later_is_kept():
    body = BODY.replace("Device\n", "What's your budget?\n$500\n\nDevice\n")
    assert webform.parse(SUBJECT, body)["other"] == [("What's your budget", "$500")]


def test_other_godaddy_emails_are_not_forms():
    assert webform.parse("You have a new chat message", BODY) is None


# --- Making the request ----------------------------------------------------------------


def test_a_form_email_becomes_a_new_request():
    received = timezone.now() - timedelta(minutes=3)
    inquiry = webform.handle(message(received=received))
    assert inquiry.channel == Channel.WEBSITE and inquiry.status == Inquiry.Status.NEW
    assert (inquiry.from_name, inquiry.from_email, inquiry.from_phone) == (
        "Pat Doe",
        "pat@example.com",
        "4035550100",
    )
    assert inquiry.subject == "100 shirts"
    assert inquiry.details.startswith("100 shirts\nTwo colours")
    assert "Sent from the website form (Contact Us)." in inquiry.details
    assert abs(inquiry.received_at - received) < timedelta(seconds=1)
    assert inquiry.customer is None


def test_the_due_date_goes_in_the_details():
    body = BODY.replace("Expected completion date?\n", "Expected completion date?\nDec 5\n")
    inquiry = webform.handle(message(body=body))
    assert "Expected completion date: Dec 5" in inquiry.details


def test_a_known_customer_is_set():
    pat = Customer.objects.create(display_name="Pat Doe", email="PAT@example.com")
    assert webform.handle(message()).customer == pat


def test_two_customers_with_the_email_are_left_to_staff():
    Customer.objects.create(display_name="Pat Doe", email="pat@example.com")
    Customer.objects.create(display_name="Pat (school)", email="pat@example.com")
    assert webform.handle(message()).customer is None


def test_each_email_is_made_once():
    assert webform.handle(message()) is not None
    assert webform.handle(message()) is None
    assert Inquiry.objects.count() == 1


def test_a_mistyped_email_is_kept_in_the_details():
    inquiry = webform.handle(message(body=BODY.replace("pat@example.com", "pat at example")))
    assert inquiry.from_email == "" and "Email (as typed): pat at example" in inquiry.details


def test_no_description_still_makes_a_request():
    body = BODY.replace("100 shirts\nTwo colours, front and back\n", "")
    inquiry = webform.handle(message(body=body))
    assert inquiry.subject == "Website enquiry"


# --- Reading the mailbox ---------------------------------------------------------------


@pytest.fixture
def mailbox(settings, monkeypatch):
    settings.WEBFORM_MAILBOX = "info@graphnix.com"
    settings.M365_CONFIGURED = True
    inbox = []

    def list_messages(mailbox, *, senders, since):
        assert mailbox == "info@graphnix.com" and SENDER in senders
        return iter(m for m in inbox if m["receivedDateTime"] >= f"{since:%Y-%m-%dT%H:%M:%S}")

    monkeypatch.setattr(graph, "list_messages", list_messages)
    return inbox


def test_the_check_makes_requests_and_remembers_how_far_it_read(mailbox):
    mailbox += [message("m1"), message("m2", subject="New chat message")]
    result = webform.check_mail()
    assert result["read"] == 1 and result["error"] == "" and result["newest"]
    assert webform.check_mail()["read"] == 0  # read again, made once
    assert health.check_webform_mail().status == health.OK


def test_a_mailbox_error_shows_on_the_health_panel(mailbox, monkeypatch):
    def broken(*a, **kw):
        raise graph.GraphError("Reading info@graphnix.com failed (403)")

    monkeypatch.setattr(graph, "list_messages", broken)
    assert "403" in webform.check_mail()["error"]
    assert health.check_webform_mail().status == health.FAIL


def test_no_mailbox_means_off(settings):
    settings.WEBFORM_MAILBOX = ""
    settings.M365_CONFIGURED = True
    assert not webform.enabled()


# --- Staff see it ----------------------------------------------------------------------


def test_new_requests_are_counted_in_the_header(client, manager):
    webform.handle(message())
    client.force_login(manager)
    html = client.get(reverse("sales:list")).content.decode()
    assert "Website Form" in html and "Pat Doe" in html and "100 shirts" in html
    assert 'title="New requests waiting">1</span>' in html
