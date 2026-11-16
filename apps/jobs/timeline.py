"""The job page's Timeline tab (FR-32): status changes, notes and edits, newest first.

Steps 2-4 add proofs, purchase orders, payments and emails sent to the same list.
"""

from dataclasses import dataclass
from datetime import datetime

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.utils.text import capfirst

from apps.audit.models import AuditLog

from .models import Job, Status

KIND_STATUS = "status"
KIND_NOTE = "note"
KIND_EDIT = "edit"

# Shown elsewhere on the timeline, so their audit entries would be duplicates.
_SKIP = {("jobs.job", "create"), ("jobs.job", "status_change"), ("jobs.job", "override")}
_NOUN = {
    "jobs.job": "Job details",
    "jobs.jobitem": "Item",
    "jobs.jobfile": "File",
    "production.productiontask": "Task",
}
_VERB = {"create": "added", "update": "changed", "delete": "removed"}
# Context keys recorded with an edit, not fields that changed.
_CONTEXT = {"line", "task", "description", "file"}
# Money from Square: only for people who can see money (NFR-02).
_MONEY = {"integrations.squareinvoice", "integrations.squareorder"}
_MONEY_ACTIONS = {"payment"}


@dataclass
class Entry:
    at: datetime
    kind: str
    who: str
    title: str
    detail: str = ""


def _label(entity_type: str, field: str) -> str:
    try:
        model = apps.get_model(entity_type)
        return capfirst(str(model._meta.get_field(field).verbose_name))
    except (LookupError, FieldDoesNotExist):  # a field since removed still shows
        return capfirst(field.replace("_", " "))


def _show(value) -> str:
    if value in (None, ""):
        return "(blank)"
    if value is True:
        return "yes"
    if value is False:
        return "no"
    if isinstance(value, dict):
        return ", ".join(f"{k} {v}" for k, v in value.items())
    return str(value)


def _edit_entry(entry: AuditLog) -> Entry:
    changes = entry.changes or {}
    noun = _NOUN.get(entry.entity_type, entry.entity_type)
    name = ""
    for key in ("task", "description", "file"):
        if key in changes:
            old, new = changes[key]
            name = new or old or ""
            break
    if "line" in changes and entry.entity_type == "jobs.jobitem":
        line = changes["line"][0] or changes["line"][1]
        noun = f"Item {line}"
    title = f"{noun} {_VERB.get(entry.action, entry.action)}"
    if name:
        title += f": {name}"
    details = [
        f"{_label(entry.entity_type, field)}: {_show(old)} → {_show(new)}"
        for field, (old, new) in changes.items()
        if field not in _CONTEXT and entry.action == "update"
    ]
    who = str(entry.actor) if entry.actor else "System"
    return Entry(entry.at, KIND_EDIT, who, title, "; ".join(details))


def _square_entry(entry: AuditLog) -> Entry:
    changes = entry.changes or {}
    number = (changes.get("invoice") or [None, None])[1] or ""
    label = f"Square invoice {number}".strip()
    if changes.get("order"):
        label = f"store order {changes['order'][1]}"
    if entry.action in ("link", "unlink"):
        who = str(entry.actor) if entry.actor else "Square"
        number = changes.get("invoice") or [None, None]
        label = f"Square invoice {number[1] or number[0] or ''}".strip()
        return Entry(entry.at, KIND_EDIT, who, f"{label} {entry.action}ed", "")
    details = [
        f"{name.capitalize()}: {_show(old)} → {_show(new)}"
        for name, (old, new) in changes.items()
        if name in ("payment", "paid", "total")
    ]
    who = str(entry.actor) if entry.actor else "Square"
    return Entry(entry.at, KIND_EDIT, who, f"Payment ({label})", "; ".join(details))


def build(job: Job, *, show_money: bool = False) -> list[Entry]:
    """The job's timeline. Payments and invoices only with `show_money`."""
    entries = []
    for row in job.history.select_related("changed_by"):
        if row.from_status:
            title = f"{Status(row.from_status).label} → {Status(row.to_status).label}"
        else:
            title = f"Created at {Status(row.to_status).label}"
        who = str(row.changed_by) if row.changed_by else row.get_source_display()
        entries.append(Entry(row.changed_at, KIND_STATUS, who, title, row.note))
    for note in job.notes.select_related("author"):
        who = str(note.author) if note.author else "System"
        entries.append(Entry(note.created_at, KIND_NOTE, who, "Note", note.body))
    for entry in AuditLog.objects.filter(job_id=job.pk).select_related("actor"):
        if (entry.entity_type, entry.action) in _SKIP or entry.entity_type == "jobs.jobnote":
            continue
        if entry.entity_type in _MONEY or entry.action in _MONEY_ACTIONS:
            if show_money:
                entries.append(_square_entry(entry))
            continue
        entries.append(_edit_entry(entry))
    entries.sort(key=lambda e: e.at, reverse=True)
    return entries
