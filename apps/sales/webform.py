"""Requests from the website's contact form, read from the mailbox the form emails.

The graphnix.com site (GoDaddy) emails each form sent to info@ from
notifications@mail.conversations.godaddy.com, subject "New contact form message for
Graphnix.com via Contact Us", with the answers as label / value pairs:

    Name / Email / Phone / Expected completion date? / Describe your project / ...

Each one becomes a new request (channel: Website Form) under Requests. The customer is set
when exactly one customer has the email; otherwise staff pick or create one as for any
request. Only that sender's form emails are opened; other mail in the box is listed by
sender only.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.audit.services import record

from .models import Channel, Inquiry

log = logging.getLogger(__name__)

RESULT_KEY = "webform_mail"
# Read again from a day before the newest form read, in case one arrived late.
OVERLAP = timedelta(days=1)

_SUBJECT = re.compile(r"new contact form message for (?P<site>.+?) via (?P<form>.+?)\s*$", re.I)
_START = "received a new message"
_END = ("open conversations", "want to forward", "invite them to join")

# The form's questions; "" ones are kept only in the details.
LABELS = {
    "name": "name",
    "email": "email",
    "phone": "phone",
    "expected completion date": "due",
    "describe your project": "project",
    "device": "",
    "language": "",
    "submitted_from": "",
    "submitted from": "",
}
_SHOWN = {"due": "Expected completion date"}


def _label(line: str) -> str | None:
    key = " ".join(line.casefold().rstrip("?: ").split())
    if key in LABELS:
        return key
    return None


def parse(subject: str, text: str) -> dict | None:
    """The form's answers, or None if this isn't a contact form email.

    {"form": "Contact Us", "name": ..., "email": ..., "phone": ..., "due": ...,
     "project": ..., "other": [(question, answer), ...]}"""
    m = _SUBJECT.search(" ".join((subject or "").split()))
    if not m:
        return None
    lines = [line.strip() for line in (text or "").splitlines()]
    starts = [i for i, line in enumerate(lines) if _START in line.casefold()]
    if starts:
        lines = lines[starts[0] + 1 :]
    answers: dict[str, list[str]] = {}
    order: list[str] = []
    current = None
    for line in lines:
        if not line:
            continue
        if line.casefold().startswith(_END):
            break
        inline = re.match(r"^([^:]{2,40}):\s*(.+)$", line)
        value = None
        if _label(line):
            current = _label(line)
        elif inline and _label(inline.group(1)):  # "Phone: 403..." on one line
            current, value = _label(inline.group(1)), inline.group(2).strip()
        elif line.endswith("?") and len(line) <= 80:
            current = line.rstrip("?").strip()  # a question the form gained later
        elif current is not None:
            value = line
        else:
            continue  # before the first question
        if current not in answers:
            answers[current] = []
            order.append(current)
        if value:
            answers[current].append(value)

    def answer(key):
        return "\n".join(answers.get(key, [])).strip()

    found = {"form": m.group("form").strip(), "other": []}
    for key in order:
        field = LABELS.get(key)
        if field:
            found[field] = answer(key)
        elif field is None and answer(key):  # not a known question
            found["other"].append((key[:1].upper() + key[1:], answer(key)))
    for field in ("name", "email", "phone", "due", "project"):
        found.setdefault(field, "")
    return found


def _details(found: dict) -> str:
    parts = [found["project"] or "(No project description given.)"]
    if found["due"]:
        parts.append(f"{_SHOWN['due']}: {found['due']}")
    parts += [f"{q}: {a}" for q, a in found["other"]]
    parts.append(f"Sent from the website form ({found['form']}).")
    return "\n\n".join(parts)


def _subject(found: dict) -> str:
    first = (found["project"].splitlines() or [""])[0].strip()
    if not first:
        return "Website enquiry"
    return first if len(first) <= 120 else first[:120].rsplit(" ", 1)[0] + "…"


def _customer(email: str):
    from apps.customers.models import Customer

    if not email:
        return None
    match = list(Customer.objects.filter(email__iexact=email)[:2])
    return match[0] if len(match) == 1 else None


def handle(message: dict) -> Inquiry | None:
    """The request for one email, made once (the message ID is kept); None if the email
    isn't a contact form or was handled already."""
    message_id = message.get("id") or ""
    if not message_id or Inquiry.objects.filter(graph_message_id=message_id).exists():
        return None
    found = parse(message.get("subject") or "", (message.get("body") or {}).get("content", ""))
    if found is None:
        return None
    email = found["email"].strip()
    try:
        validate_email(email)
    except ValidationError:
        if email:
            found["other"].append(("Email (as typed)", email))
        email = ""
    received = parse_datetime(message.get("receivedDateTime") or "") or timezone.now()
    try:
        with transaction.atomic():
            inquiry = Inquiry.objects.create(
                channel=Channel.WEBSITE,
                received_at=received,
                customer=_customer(email),
                from_name=found["name"][:200],
                from_email=email,
                from_phone=found["phone"][:40],
                subject=_subject(found)[:200],
                details=_details(found),
                graph_message_id=message_id[:255],
            )
    except IntegrityError:  # the other check got there first
        return None
    record(None, inquiry, "create", {"channel": [None, inquiry.channel]})
    return inquiry


def enabled() -> bool:
    return bool(settings.WEBFORM_MAILBOX and settings.M365_CONFIGURED)


def check_mail(*, since=None) -> dict:
    """Read the contact form emails received since `since` (default: a day before the newest
    one read) and make a request for each new one. The result is kept for the health panel."""
    from apps.accounts.models import AppSetting, get_setting
    from apps.m365 import graph, mailcheck

    now = timezone.now()
    result = {"at": now.isoformat(), "read": 0, "error": ""}
    last = get_setting(RESULT_KEY) or {}
    if since is None:
        newest = parse_datetime(last.get("newest") or "")
        since = (newest - OVERLAP) if newest else now - OVERLAP
    result["newest"] = last.get("newest", "")
    try:
        for message in graph.list_messages(
            settings.WEBFORM_MAILBOX, senders=settings.WEBFORM_SENDERS, since=since
        ):
            try:
                inquiry = handle(message)
            except Exception:
                log.exception("Website form email %s couldn't be handled", message.get("id"))
                result["error"] = "A website form email couldn't be handled; see the crash email."
                continue
            if inquiry is not None:
                result["read"] += 1
            received = message.get("receivedDateTime") or ""
            if received > result["newest"]:
                result["newest"] = received
    except graph.GraphError as exc:
        mailcheck.failed(result, last, exc, log=log, what="website form emails", now=now)
    AppSetting.objects.update_or_create(key=RESULT_KEY, defaults={"value": result})
    return result


def last_result() -> dict | None:
    from apps.accounts.models import get_setting

    return get_setting(RESULT_KEY)
