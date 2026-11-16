"""Payment gates (FR-07): stages a job may not move into until Square shows a deposit or
full payment. Set by an Admin; the default is the owners' answer to question 4: paid in
full before Materials ordered. Only on servers where Square is set up, so a server without
Square never blocks a job.

A gate is checked on every forward move that reaches or passes its stage, so skipping
ahead can't avoid it. Moving backward is never gated, and a refund never moves a job back.
Someone with `integrations.override_payment_gate` (Admin) can move past it with a reason.
"""

from django.conf import settings

from apps.accounts.models import get_setting

from .flow import PATH
from .models import PaymentStatus, Status

SETTING = "payment_gates"
DEFAULT = {Status.MATERIALS_ORDERED.value: "paid"}
LEVELS = {"deposit": "a deposit", "paid": "payment in full"}
MET = {
    "deposit": {PaymentStatus.DEPOSIT, PaymentStatus.PAID},
    "paid": {PaymentStatus.PAID},
}
OVERRIDE = "integrations.override_payment_gate"
# Gates can be set on the stages after Invoiced.
GATEABLE = PATH[PATH.index(Status.INVOICED) + 1 : PATH.index(Status.CLOSED)]


def configured() -> dict[str, str]:
    """The saved gates, whether or not Square is on."""
    saved = get_setting(SETTING, DEFAULT) or {}
    return {s: lvl for s, lvl in saved.items() if s in GATEABLE and lvl in LEVELS}


def current() -> dict[str, str]:
    return configured() if settings.SQUARE_ENABLED else {}


# Said to people who can't see money (NFR-02): no payment status, no amounts.
HIDDEN = "The office needs to move this job on"


def blocking(job, to_status: str, user=None) -> str:
    """Why this move is blocked by a payment gate, or '' if it isn't. Without
    `jobs.view_money`, the reason doesn't mention payment."""
    if job.status not in PATH or to_status not in PATH:
        return ""
    start, end = PATH.index(job.status), PATH.index(to_status)
    if end <= start:
        return ""
    gates = current()
    for stage in PATH[start + 1 : end + 1]:
        level = gates.get(stage)
        if level and job.payment_status not in MET[level]:
            if user is not None and not user.has_perm("jobs.view_money"):
                return HIDDEN
            return (
                f"Needs {LEVELS[level]} before {Status(stage).label}: "
                f"{job.get_payment_status_display()}"
            )
    return ""
