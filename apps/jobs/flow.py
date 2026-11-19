"""The job state machine's rules (FR-30), as data. `services` enforces them.

See the Step 1 spec, "Job state machine":
- A job starts at Quoting (quote first) or at Invoiced (straight to invoice).
- Forward one stage: the permission of the lane the job is leaving.
- Any other move along the path (backward, or skipping ahead): Admin or Manager, with a
  reason. Skip mock-up and reopen mock-up are named cases of this.
- On hold from any open stage; Cancelled from any stage before Shipped. Admin or Manager.
- Closed is reached only from Shipped and is final.
"""

from .models import Status

PATH = [
    Status.QUOTING,
    Status.QUOTE_SENT,
    Status.INVOICED,  # also "quote approved", and the mock-up being drawn
    Status.MOCKUP_SENT,
    Status.MOCKUP_APPROVED,
    Status.MATERIALS_ORDERED,
    Status.READY_FOR_PRODUCTION,
    Status.IN_PRODUCTION,
    Status.QA,
    Status.READY,
    Status.SHIPPED,
    Status.CLOSED,
]

START_STATUSES = {Status.QUOTING, Status.INVOICED}
# A sent Square invoice moves a job from these to Invoiced (FR-06).
BEFORE_INVOICED = {Status.QUOTING, Status.QUOTE_SENT}

OFFICE = "jobs.move_office_stages"
PRODUCTION = "jobs.move_production_stages"
FINISH = "jobs.move_finish_stages"
OVERRIDE = "jobs.override_job_flow"
REOPEN = "jobs.reopen_cancelled_job"

# Who may move a job forward out of each stage (the lanes in the spec's diagram).
LANE = {
    Status.QUOTING: OFFICE,
    Status.QUOTE_SENT: OFFICE,
    Status.INVOICED: OFFICE,
    Status.MOCKUP_SENT: OFFICE,
    Status.MOCKUP_APPROVED: OFFICE,
    Status.MATERIALS_ORDERED: OFFICE,
    Status.READY_FOR_PRODUCTION: PRODUCTION,
    Status.IN_PRODUCTION: PRODUCTION,
    Status.QA: FINISH,
    Status.READY: FINISH,
    Status.SHIPPED: FINISH,
}

# Stages a job can be put on hold from ("any open stage").
HOLDABLE = set(PATH) - {Status.SHIPPED, Status.CLOSED}
# Stages a job can be cancelled from ("any stage before Shipped").
CANCELLABLE = HOLDABLE | {Status.ON_HOLD}

# Mock-up Pending too: a paid invoice moves the job there by itself.
SKIP_MOCKUP_FROM = {Status.INVOICED, Status.MOCKUP_SENT}
SKIP_MOCKUP_TO = Status.MATERIALS_ORDERED
REOPEN_MOCKUP_FROM = {
    Status.MOCKUP_APPROVED,
    Status.MATERIALS_ORDERED,
    Status.READY_FOR_PRODUCTION,
}
# Back to drawing: the stage before Mock-up Pending.
REOPEN_MOCKUP_TO = Status.INVOICED

FORWARD = "forward"
JUMP = "jump"  # backward, or skipping a stage: needs OVERRIDE and a reason


class InvalidMove(Exception):
    """The state machine does not allow this move at all."""


def next_status(status: str) -> str | None:
    if status not in PATH or status == Status.CLOSED:
        return None
    return PATH[PATH.index(status) + 1]


def classify(from_status: str, to_status: str) -> str:
    """FORWARD or JUMP, or raise InvalidMove. Holds and cancels have their own services."""
    if from_status not in PATH or to_status not in PATH:
        raise InvalidMove(
            f"Use hold, cancel or reopen for moves from {from_status} to {to_status}."
        )
    if from_status == Status.CLOSED:
        raise InvalidMove("A closed job can't be moved.")
    if from_status == to_status:
        raise InvalidMove("The job is already at that stage.")
    if to_status == next_status(from_status):
        return FORWARD
    if to_status == Status.CLOSED:
        raise InvalidMove("Only a shipped job can be closed. Cancel it instead.")
    return JUMP


def permission_for(kind: str, from_status: str) -> str:
    return LANE[from_status] if kind == FORWARD else OVERRIDE
