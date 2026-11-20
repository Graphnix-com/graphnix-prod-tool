"""Microsoft 365 not answering for a moment: retried quietly, emailed only if it lasts."""

import logging
from datetime import timedelta
from unittest import mock

import pytest
import requests
from django.core import mail
from django.utils import timezone

from apps.core import health
from apps.integrations import estimates
from apps.m365 import graph, mailcheck
from apps.sales import webform

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    waits = []
    monkeypatch.setattr(graph, "_sleep", waits.append)
    monkeypatch.setattr(graph, "get_token", lambda: "t")
    return waits


def ok(data=None, status=200):
    return mock.Mock(status_code=status, headers={}, json=lambda: data or {"value": []})


def test_a_timeout_is_tried_again(monkeypatch, no_waiting):
    get = mock.Mock(side_effect=[requests.ReadTimeout("slow"), ok()])
    monkeypatch.setattr(graph.requests, "get", get)
    assert list(graph.list_messages("info@graphnix.com", senders=[], since=timezone.now())) == []
    assert get.call_count == 2 and no_waiting == [2]


def test_busy_answers_are_tried_again_honouring_retry_after(monkeypatch, no_waiting):
    busy = mock.Mock(status_code=503, headers={"Retry-After": "9"})
    monkeypatch.setattr(graph.requests, "get", mock.Mock(side_effect=[busy, ok()]))
    list(graph.list_messages("info@graphnix.com", senders=[], since=timezone.now()))
    assert no_waiting == [9]


def test_three_timeouts_become_graph_unavailable(monkeypatch):
    get = mock.Mock(side_effect=requests.ReadTimeout("slow"))
    monkeypatch.setattr(graph.requests, "get", get)
    with pytest.raises(graph.GraphUnavailable, match="didn't answer"):
        list(graph.list_messages("info@graphnix.com", senders=[], since=timezone.now()))
    assert get.call_count == 3


def test_a_refusal_is_not_tried_again(monkeypatch):
    get = mock.Mock(return_value=mock.Mock(status_code=403, headers={}, text="Denied"))
    monkeypatch.setattr(graph.requests, "get", get)
    with pytest.raises(graph.GraphError) as caught:
        list(graph.list_messages("info@graphnix.com", senders=[], since=timezone.now()))
    assert not isinstance(caught.value, graph.GraphUnavailable) and get.call_count == 1


def test_an_email_is_not_sent_again_after_a_read_timeout(monkeypatch):
    # The email may already have gone out: sending it again could send it twice.
    post = mock.Mock(side_effect=requests.ReadTimeout("slow"))
    monkeypatch.setattr(graph.requests, "post", post)
    with pytest.raises(graph.GraphUnavailable):
        graph.send_mail(sender="info@graphnix.com", to=["a@example.com"], subject="s", body="b")
    assert post.call_count == 1


def test_an_email_is_sent_again_when_graph_was_never_reached(monkeypatch):
    accepted = mock.Mock(status_code=202, headers={}, text="")
    post = mock.Mock(side_effect=[requests.ConnectTimeout("no route"), accepted])
    monkeypatch.setattr(graph.requests, "post", post)
    graph.send_mail(sender="info@graphnix.com", to=["a@example.com"], subject="s", body="b")
    assert post.call_count == 2


def test_a_sign_in_timeout_is_graph_unavailable(monkeypatch):
    monkeypatch.undo()  # the real get_token
    client = mock.Mock()
    client.acquire_token_for_client.side_effect = requests.ReadTimeout("slow")
    monkeypatch.setattr(graph, "_client", lambda: client)
    with pytest.raises(graph.GraphUnavailable):
        graph.get_token()


# --- The 5-minute checks -----------------------------------------------------------------


@pytest.fixture
def down(settings, monkeypatch):
    settings.ESTIMATE_MAILBOX = settings.WEBFORM_MAILBOX = "info@graphnix.com"
    settings.M365_CONFIGURED = True
    settings.ADMINS = [("Admin", "admin@example.com")]
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    state = {"error": graph.GraphUnavailable("Microsoft 365 didn't answer (ReadTimeout)")}

    def list_messages(*a, **kw):
        if state["error"]:
            raise state["error"]
        yield from ()

    monkeypatch.setattr(graph, "list_messages", list_messages)
    return state


@pytest.mark.parametrize(
    "module, check",
    [
        (estimates, health.check_estimate_mail),
        (webform, health.check_webform_mail),
    ],
)
def test_a_short_outage_sends_no_crash_email(down, module, check, caplog):
    caplog.set_level(logging.WARNING)
    module.check_mail()
    assert mail.outbox == []
    assert check().status == health.WARN and "Trying again" in check().detail
    down["error"] = None
    module.check_mail()
    assert check().status == health.OK and mail.outbox == []


def test_an_outage_lasting_30_minutes_sends_one_email(down, monkeypatch):
    start = timezone.now()
    for minutes in range(0, 50, 5):
        monkeypatch.setattr(
            estimates.timezone, "now", lambda m=minutes: start + timedelta(minutes=m)
        )
        estimates.check_mail()
    emailed = [m for m in mail.outbox if "over 30 minutes" in m.subject]
    assert len(emailed) == 1 and len(mail.outbox) == 1
    later = start + timedelta(minutes=45)
    assert health.check_estimate_mail(now=later).status == health.FAIL


def test_a_refused_permission_is_emailed_straight_away(down):
    down["error"] = graph.GraphError("Reading info@graphnix.com failed (403)")
    estimates.check_mail()
    assert len(mail.outbox) == 1
    assert health.check_estimate_mail().status == health.FAIL


def test_brief_outage_needs_a_start_time():
    assert not mailcheck.is_brief_outage({"error": "x"}, timezone.now())
