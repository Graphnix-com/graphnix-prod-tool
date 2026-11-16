"""Minimal Microsoft Graph client: app-only token and sendMail.

The app registration needs the Graph *application* permission Mail.Send with admin
consent, limited by Exchange RBAC for Applications to info@ and tracker-test@.
"""

from __future__ import annotations

import threading
from datetime import UTC

import msal
import requests
from django.conf import settings

GRAPH_URL = "https://graph.microsoft.com/v1.0"
SCOPES = ["https://graph.microsoft.com/.default"]
TIMEOUT_SECONDS = 15


class GraphError(RuntimeError):
    pass


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
    result = _client().acquire_token_for_client(scopes=SCOPES)
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

    response = requests.post(
        f"{GRAPH_URL}/users/{sender}/sendMail",
        headers={"Authorization": f"Bearer {get_token()}"},
        json={"message": message, "saveToSentItems": save_to_sent_items},
        timeout=TIMEOUT_SECONDS,
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
        response = requests.get(url, headers=headers, params=params, timeout=TIMEOUT_SECONDS)
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
        response = requests.get(
            f"{GRAPH_URL}/users/{mailbox}/messages/{message['id']}",
            headers=text,
            params={"$select": "id,subject,from,receivedDateTime,body"},
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise GraphError(f"Reading a message failed ({response.status_code})")
        yield response.json()
