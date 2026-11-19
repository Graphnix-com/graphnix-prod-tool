"""Shop notes: shared to-dos on the board (staff feedback, 19 Nov 2026).

Anyone who can see the board adds one, about a job or general; each keeps who added it and
when. Managers and Admins tick a note off (it leaves the list, kept for the record) or
remove it outright. Production sees the board, so a note can't carry a price.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.audit.services import record

from . import board, money
from .models import ShopNote

ADD = "jobs.add_shopnote"
TICK = "jobs.change_shopnote"
REMOVE = "jobs.delete_shopnote"


def open_notes():
    return ShopNote.objects.filter(done_at__isnull=True).select_related("job", "created_by")


def add(*, user, text: str, job_number: str = "") -> ShopNote:
    if not user.has_perm(ADD):
        raise PermissionDenied("You can't add shop notes.")
    text = " ".join((text or "").split())[:300]
    if not text:
        raise ValidationError("Write the note first.")
    if money.has_amount(text):
        raise ValidationError(money.MESSAGE)
    job = None
    if (job_number or "").strip():
        job = board.exact_number(job_number)
        if job is None:
            raise ValidationError(f"No single job has the number “{job_number.strip()}”.")
    note = ShopNote.objects.create(text=text, job=job, created_by=user)
    record(user, note, "create", {"text": [None, text]}, job=job)
    return note


def tick(note: ShopNote, *, user) -> ShopNote:
    if not user.has_perm(TICK):
        raise PermissionDenied("Only Managers and Admins can tick off shop notes.")
    if note.done_at is None:
        note.done_at = timezone.now()
        note.done_by = user
        note.save(update_fields=["done_at", "done_by"])
        record(user, note, "update", {"done": [None, note.text]}, job=note.job)
    return note


def remove(note: ShopNote, *, user) -> None:
    if not user.has_perm(REMOVE):
        raise PermissionDenied("Only Managers and Admins can remove shop notes.")
    record(user, note, "delete", {"text": [note.text, None]}, job=note.job)
    note.delete()
