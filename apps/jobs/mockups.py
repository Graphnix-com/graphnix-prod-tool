"""Mock-up versions and their approval (FR-10 to FR-12; Step 3 spec, "Mock-ups and approval").

Proofs go out and come back by email. The tracker keeps the versions (v1, v2 ...), and a
person moves the job:

- to Mock-up Pending, naming the version that went out;
- to Mock-up approved, naming the version approved and who approved it, with every box in
  the `mockup_approval_checks` setting ticked (Admin and Manager only).

The mock-up gate: a forward move past Mock-up approved without stopping there is refused,
except Skip mock-up and an Admin override with a reason. Moving into Mock-up approved needs
the ticks, so a job past the gate was either approved this way, came from an online store
(already approved), or skipped the mock-up on purpose.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max

from apps.audit.services import record

from . import money
from .flow import PATH, InvalidMove
from .models import Job, JobFile, MockupVersion, Status

ADD = "jobs.add_mockupversion"
APPROVE = "jobs.approve_mockup"
OVERRIDE = "jobs.override_mockup_gate"
# A mock-up is something a customer can look at in their email: an image or a PDF.
FILE_TYPES = {"png", "jpg", "jpeg", "gif", "webp", "pdf"}

# The value of the version picker that means "no mock-up attached": anyone who can make the
# move may go ahead without one (an override, recorded on the job).
NONE = "none"

SETTING = "mockup_approval_checks"
DEFAULT_CHECKS = [
    "The customer approved this version in writing (email)",
    "Spelling and wording checked",
    "Colours checked",
    "Placement and size of the design checked",
    "Garment sizes and quantities checked",
]

APPROVED = "approved"
WAITING = "waiting"  # sent, no answer yet
CHANGES = "changes"  # sent, and the job went back to Invoiced for a new version
DRAFT = "draft"  # added, not sent yet
SUPERSEDED = "superseded"


def checks() -> list[str]:
    """The boxes to tick before Mock-up approved (app setting, else the five agreed)."""
    from apps.accounts.models import get_setting

    saved = get_setting(SETTING)
    if isinstance(saved, list) and saved and all(isinstance(c, str) and c for c in saved):
        return saved
    return list(DEFAULT_CHECKS)


# --- Versions --------------------------------------------------------------------


def add_version(job: Job, *, user, file: JobFile | None = None, upload=None, note: str = ""):
    """The next version (v1, v2 ...) from a file on the job, or a new upload."""
    from . import files

    if not user.has_perm(ADD):
        raise PermissionDenied("Only Admin and Manager can add mock-up versions.")
    note = (note or "").strip()[:300]
    if money.has_amount(note):
        raise ValidationError(money.MESSAGE)
    if upload is not None:
        _check_type(upload.name)
        file = files.add(job, [upload], user=user)[0]
    if file is None:
        raise ValidationError("Pick a file on the job, or upload one.")
    if file.job_id != job.pk:
        raise ValidationError("That file is on another job.")
    _check_type(file.name)
    with transaction.atomic():
        Job.objects.select_for_update().filter(pk=job.pk).first()
        last = job.mockups.aggregate(n=Max("version"))["n"] or 0
        mockup = MockupVersion.objects.create(
            job=job, version=last + 1, file=file, note=note, created_by=user
        )
        changes = {"mockup": [None, str(mockup)], "file": [None, file.name]}
        record(user, mockup, "create", changes, job=job)
    return mockup


def _check_type(name: str) -> None:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in FILE_TYPES:
        raise ValidationError("A mock-up must be an image (PNG, JPG, GIF, WebP) or a PDF.")


# --- The moves -------------------------------------------------------------------


def _version(job: Job, mockup_id) -> MockupVersion | None:
    try:
        return job.mockups.select_related("file").get(pk=int(mockup_id))
    except (TypeError, ValueError, MockupVersion.DoesNotExist):
        return None


def for_sent(job: Job, mockup_id) -> MockupVersion | None:
    """The version named when a person moves the job to Mock-up Pending; None when they
    chose to go ahead with no mock-up attached."""
    if mockup_id == NONE:
        return None
    mockup = _version(job, mockup_id)
    if mockup is None:
        if not job.mockups.exists():
            raise InvalidMove(
                "Add the mock-up as a version first (Mock-ups tab), or choose No mock-up attached."
            )
        raise InvalidMove("Pick the mock-up version that went to the customer.")
    return mockup


def for_approval(job: Job, user, *, mockup_id, approved_by: str = "", ticked=()) -> dict:
    """What a person must give to move the job to Mock-up approved."""
    if not user.has_perm(APPROVE):
        raise PermissionDenied("Only Admin and Manager can mark a mock-up approved.")
    mockup = None if mockup_id == NONE else _version(job, mockup_id)
    if mockup is None and mockup_id != NONE:
        raise InvalidMove("Pick the mock-up version the customer approved.")
    approved_by = " ".join((approved_by or "").split())[:200]
    if not approved_by:
        raise InvalidMove("Say who approved it, as in their email.")
    required = checks()
    missing = [c for c in required if c not in set(ticked or ())]
    if missing:
        raise InvalidMove("Tick every box before approving. Not ticked: " + "; ".join(missing))
    return {"mockup": mockup, "approved_by": approved_by, "checks": required}


def approved_by_default(job: Job) -> str:
    """What the Approved by box starts with: the job's contact, else the customer, with
    their email when known. Staff change it if someone else approved."""
    person = job.contact or job.customer
    name = getattr(person, "full_name", "") or getattr(person, "display_name", "")
    email = person.email or (job.customer.email if job.contact else "")
    return f"{name} ({email})" if name and email else name or email


def crosses_approval(from_status: str, to_status: str) -> bool:
    """A forward move that passes Mock-up approved without stopping there."""
    if from_status not in PATH or to_status not in PATH:
        return False
    gate = PATH.index(Status.MOCKUP_APPROVED)
    return PATH.index(from_status) < gate < PATH.index(to_status)


# --- What the Mock-ups tab shows ---------------------------------------------------


def states(job: Job) -> list[dict]:
    """Every version with its state, the approved one first, then newest first.

    The approval stands until a newer version is sent. Versions newer than the approved (or
    the last sent) one and not yet sent are drafts; older ones are superseded."""
    versions = list(job.mockups.select_related("file", "created_by"))
    approval = job.mockup_approvals.select_related("mockup", "saved_by").first()
    sent = [v for v in versions if v.sent_at]
    latest_sent = max(sent, key=lambda v: v.sent_at) if sent else None
    if approval is not None and latest_sent and latest_sent.sent_at > approval.approved_at:
        approval = None  # a newer version went out: that one is waiting now
    reference = approval.mockup if approval is not None and approval.mockup else latest_sent
    rows = []
    for v in versions:
        if approval is not None and v.pk == approval.mockup_id:
            state = APPROVED
        elif reference is None or v.version > reference.version:
            state = DRAFT
        elif v.pk == reference.pk:
            state = WAITING if _waiting(job) else CHANGES
        else:
            state = SUPERSEDED
        rows.append(
            {"version": v, "state": state, "approval": approval if state == APPROVED else None}
        )
    rows.sort(key=lambda r: (r["state"] != APPROVED, -r["version"].version))
    return rows


def _waiting(job: Job) -> bool:
    """The job is at Mock-up Pending (or on hold from there): the customer hasn't answered."""
    if job.status == Status.ON_HOLD:
        return job.resume_status == Status.MOCKUP_SENT
    return job.status == Status.MOCKUP_SENT


def approved(job: Job) -> MockupVersion | None:
    """The version the floor should print, if one is approved."""
    return next((r["version"] for r in states(job) if r["state"] == APPROVED), None)
