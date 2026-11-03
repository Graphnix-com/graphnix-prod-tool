import logging
from unittest import mock

import pytest
from django.core import mail
from django.core.mail import EmailMultiAlternatives

from apps.m365 import graph
from apps.m365.email_backend import GraphEmailBackend

pytestmark = pytest.mark.django_db


@pytest.fixture
def graph_post(monkeypatch):
    monkeypatch.setattr(graph, "get_token", lambda: "token-123")
    post = mock.Mock(return_value=mock.Mock(status_code=202, text=""))
    monkeypatch.setattr(graph.requests, "post", post)
    return post


def test_backend_sends_plain_text(settings, graph_post):
    settings.M365_SENDER = "tracker-test@graphnix.com"
    message = EmailMultiAlternatives("Hi", "Body", to=["c@example.com"], cc=["d@example.com"])
    assert GraphEmailBackend().send_messages([message]) == 1

    url = graph_post.call_args.args[0]
    payload = graph_post.call_args.kwargs["json"]["message"]
    assert url.endswith("/users/tracker-test@graphnix.com/sendMail")
    assert graph_post.call_args.kwargs["headers"]["Authorization"] == "Bearer token-123"
    assert payload["body"] == {"contentType": "Text", "content": "Body"}
    assert payload["toRecipients"] == [{"emailAddress": {"address": "c@example.com"}}]
    assert payload["ccRecipients"] == [{"emailAddress": {"address": "d@example.com"}}]


def test_backend_prefers_html_alternative(graph_post):
    message = EmailMultiAlternatives("Hi", "plain", to=["c@example.com"])
    message.attach_alternative("<p>html</p>", "text/html")
    GraphEmailBackend().send_messages([message])
    body = graph_post.call_args.kwargs["json"]["message"]["body"]
    assert body == {"contentType": "HTML", "content": "<p>html</p>"}


def test_backend_raises_or_swallows_errors(graph_post):
    graph_post.return_value = mock.Mock(status_code=403, text="ErrorAccessDenied")
    message = EmailMultiAlternatives("Hi", "Body", to=["c@example.com"])
    with pytest.raises(graph.GraphError):
        GraphEmailBackend().send_messages([message])
    assert GraphEmailBackend(fail_silently=True).send_messages([message]) == 0


def test_token_error_is_explained(monkeypatch):
    client = mock.Mock()
    client.acquire_token_for_client.return_value = {
        "error": "invalid_client",
        "error_description": "bad secret",
    }
    monkeypatch.setattr(graph, "_client", lambda: client)
    with pytest.raises(graph.GraphError, match="bad secret"):
        graph.get_token()


def _log_crash(logger_name="django.request", msg="Internal Server Error: /x"):
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger(logger_name).error(msg, exc_info=True)


def test_crash_email_is_sent_once_per_fault(settings):
    settings.DEBUG = False
    settings.ADMINS = [("Admin", "josh@graphnix.com")]
    _log_crash()
    _log_crash()
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["josh@graphnix.com"]
    assert "Internal Server Error" in mail.outbox[0].subject

    _log_crash(msg="A different fault")
    assert len(mail.outbox) == 2


def test_no_crash_email_without_admins(settings):
    settings.DEBUG = False
    settings.ADMINS = []
    _log_crash()
    assert mail.outbox == []
