"""Minimal Microsoft Graph client: app-only token and sendMail.

The app registration needs the Graph *application* permission Mail.Send with admin
consent, limited by Exchange RBAC for Applications to info@ and tracker-test@.
"""

from __future__ import annotations

import contextlib
import threading
import time
from datetime import UTC

import msal
import requests
from django.conf import settings

GRAPH_URL = "https://graph.microsoft.com/v1.0"
SCOPES = ["https://graph.microsoft.com/.default"]
TIMEOUT_SECONDS = 15


# Microsoft 365 sometimes doesn't answer for a moment. Each request is tried up to 3 times,
# waiting this many seconds between tries (longer if Graph says how long with Retry-After).
RETRY_WAITS = (2, 5)
BUSY = {429, 500, 502, 503, 504}


class GraphError(RuntimeError):
    pass


class GraphUnavailable(GraphError):
    """Graph didn't answer, or said it was busy, on every try: usually over in minutes."""


def _sleep(seconds):  # tests replace this
    time.sleep(seconds)


def _request(method: str, url: str, *, safe_to_repeat: bool = True, **kwargs):
    """One Graph request, tried again when Graph is busy or doesn't answer.

    A request that isn't safe to repeat (sending an email) is tried again only when it never
    reached Graph or Graph refused it as busy, so an email can't go out twice."""
    retry_on = (
        (requests.Timeout, requests.ConnectionError)
        if safe_to_repeat
        else (requests.ConnectTimeout)
    )
    busy = BUSY if safe_to_repeat else {429, 503}
    for wait in (*RETRY_WAITS, None):
        try:
            response = getattr(requests, method)(url, timeout=TIMEOUT_SECONDS, **kwargs)
        except retry_on as exc:
            if wait is None:
                raise GraphUnavailable(
                    f"Microsoft 365 didn't answer ({type(exc).__name__})"
                ) from exc
        except requests.RequestException as exc:
            raise GraphUnavailable(f"Microsoft 365 didn't answer ({type(exc).__name__})") from exc
        else:
            if response.status_code not in busy:
                return response
            if wait is None:
                raise GraphUnavailable(f"Microsoft 365 is busy ({response.status_code})")
            with contextlib.suppress(TypeError, ValueError, AttributeError):
                wait = max(wait, min(int(response.headers.get("Retry-After", 0)), 30))
        _sleep(wait)


_app: msal.ConfidentialClientApplication | None = None
_app_lock = threading.Lock()


def _client() -> msal.ConfidentialClientApplication:
    """One MSAL client per process; it caches the token until shortly before expiry."""
    global _app
    with _app_lock:
        if _app is None:
            _app = msal.ConfidentialClientApplication(
                client_id=settings.M365_CLIENT_ID,
                client_credential=settings.M365_CLIENT_SECRET,
                authority=f"https://login.microsoftonline.com/{settings.M365_TENANT_ID}",
            )
        return _app


def get_token() -> str:
    try:
        result = _client().acquire_token_for_client(scopes=SCOPES)
    except requests.RequestException as exc:
        raise GraphUnavailable(f"Microsoft sign-in didn't answer ({type(exc).__name__})") from exc
    token = result.get("access_token")
    if not token:
        raise GraphError(
            f"Could not get a Graph token: {result.get('error')}: {result.get('error_description')}"
        )
    return token


def send_mail(
    *,
    sender: str,
    to: list[str],
    subject: str,
    body: str,
    html: bool = False,
    cc: list[str] | None = None,
    reply_to: list[str] | None = None,
    save_to_sent_items: bool = True,
) -> None:
    def recipients(addresses):
        return [{"emailAddress": {"address": a}} for a in addresses or []]

    message = {
        "subject": subject,
        "body": {"contentType": "HTML" if html else "Text", "content": body},
        "toRecipients": recipients(to),
    }
    if cc:
        message["ccRecipients"] = recipients(cc)
    if reply_to:
        message["replyTo"] = recipients(reply_to)

    response = _request(
        "post",
        f"{GRAPH_URL}/users/{sender}/sendMail",
        safe_to_repeat=False,
        headers={"Authorization": f"Bearer {get_token()}"},
        json={"message": message, "saveToSentItems": save_to_sent_items},
    )
    if response.status_code != 202:
        raise GraphError(f"sendMail failed ({response.status_code}): {response.text[:500]}")


def list_messages(mailbox: str, *, senders: list[str], since):
    """Messages in `mailbox` received since `since` from any of `senders`, oldest first,
    each with its body as plain text. Other mail is listed by sender only, never opened.

    Needs the Exchange role "Application Mail.Read" scoped to the mailbox (server setup,
    section 9)."""
    wanted = {s.lower() for s in senders}
    headers = {"Authorization": f"Bearer {get_token()}", "Prefer": 'IdType="ImmutableId"'}
    url = f"{GRAPH_URL}/users/{mailbox}/messages"
    params = {
        "$filter": f"receivedDateTime ge {since.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}",
        "$orderby": "receivedDateTime asc",
        "$select": "id,subject,from,receivedDateTime",
        "$top": "100",
    }
    matched = []
    for _ in range(50):
        response = _request("get", url, headers=headers, params=params)
        if response.status_code != 200:
            raise GraphError(
                f"Reading {mailbox} failed ({response.status_code}): {response.text[:300]}"
            )
        page = response.json()
        for message in page.get("value", []):
            sender = ((message.get("from") or {}).get("emailAddress") or {}).get("address", "")
            if sender.lower() in wanted:
                matched.append(message)
        url, params = page.get("@odata.nextLink"), None
        if not url:
            break
    text = {**headers, "Prefer": 'IdType="ImmutableId", outlook.body-content-type="text"'}
    for message in matched:
        response = _request(
            "get",
            f"{GRAPH_URL}/users/{mailbox}/messages/{message['id']}",
            headers=text,
            params={"$select": "id,subject,from,receivedDateTime,body"},
        )
        if response.status_code != 200:
            raise GraphError(f"Reading a message failed ({response.status_code})")
        yield response.json()
