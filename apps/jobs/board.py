"""Queries behind the Home page, the board and header search (FR-31, FR-33, FR-34)."""

from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import F, Prefetch, Q
from django.utils import timezone

from apps.production.models import ProductionTask

from .flags import AT_RISK, OVERDUE, Flagger
from .models import HoldReason, Job, PaymentStatus, Status, WaitingOn

# Board rows follow the lanes of the state machine diagram.
LANES = [
    ("Sales", [Status.QUOTING, Status.QUOTE_SENT, Status.INVOICED]),
    (
        "Pre-production",
        [
            Status.MOCKUP_SENT,
            Status.MOCKUP_APPROVED,
            Status.MATERIALS_ORDERED,
        ],
    ),
    ("Production", [Status.READY_FOR_PRODUCTION, Status.IN_PRODUCTION]),
    ("Finish", [Status.QA, Status.READY, Status.SHIPPED]),
]
SIDE_LANE = ("On hold and cancelled", [Status.ON_HOLD, Status.CANCELLED])


def lanes_for(user) -> list[str]:
    """The lanes this person works in, shown by default: the office sees every lane;
    Production sees Production; QA and shipping adds Finish."""
    if user.has_perm("jobs.move_office_stages"):
        return [name for name, _ in LANES]
    names = []
    if user.has_perm("jobs.move_production_stages"):
        names.append("Production")
    if user.has_perm("jobs.move_finish_stages"):
        names.append("Finish")
    return names or [name for name, _ in LANES]


CANCELLED_SHOWN_DAYS = 30

DUE_FILTERS = [
    ("overdue", "Overdue"),
    ("at_risk", "At Risk"),
    ("this_week", "Due In 7 Days"),
    ("none", "No Due Date"),
]


def base_queryset():
    tasks = ProductionTask.objects.select_related("decoration_method", "assigned_to").order_by(
        "sequence"
    )
    return Job.objects.select_related("customer").prefetch_related(
        Prefetch("tasks", queryset=tasks, to_attr="task_list")
    )


def _ordered(qs):
    return qs.order_by("priority", F("due_date").asc(nulls_last=True), "status_changed_at", "id")


@dataclass
class Card:
    job: Job
    flag: str | None
    methods: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    mine: list[str] = field(default_factory=list)  # the viewer's own tasks on this job


def card(job: Job, flagger: Flagger) -> Card:
    tasks = getattr(job, "task_list", [])
    methods = list(dict.fromkeys(t.decoration_method.name for t in tasks))
    assignees = list(
        dict.fromkeys(t.assigned_to.get_short_name() for t in tasks if t.assigned_to_id)
    )
    return Card(job=job, flag=flagger(job), methods=methods, assignees=assignees)


# --- Assigned to me ----------------------------------------------------------------


def assigned_to(user, flagger: Flagger | None = None) -> list[Card]:
    """Every open job with a task assigned to `user`, whatever its stage, soonest due first
    (no due date last). The board's filters don't apply: it's always all of their work."""
    flagger = flagger or Flagger()
    jobs = (
        base_queryset()
        .filter(tasks__assigned_to=user)
        .exclude(status__in=[Status.CLOSED, Status.CANCELLED])
        .order_by(F("due_date").asc(nulls_last=True), "priority", "status_changed_at", "id")
        .distinct()
    )
    cards = []
    for job in jobs:
        c = card(job, flagger)
        c.mine = [
            t.decoration_method.name + (f" · {t.placement}" if t.placement else "")
            for t in job.task_list
            if t.assigned_to_id == user.pk
        ]
        cards.append(c)
    return cards


# --- Filters ----------------------------------------------------------------------


@dataclass
class Filters:
    method: str = ""
    due: str = ""
    assignee: str = ""
    customer: str = ""
    payment: str = ""

    @classmethod
    def from_request(cls, params, can_see_money: bool) -> "Filters":
        return cls(
            method=params.get("method", ""),
            due=params.get("due", ""),
            assignee=params.get("assignee", ""),
            customer=params.get("customer", "").strip(),
            payment=params.get("payment", "") if can_see_money else "",
        )

    @property
    def active(self) -> bool:
        return any([self.method, self.due, self.assignee, self.customer, self.payment])

    def apply(self, qs, flagger: Flagger):
        if self.method.isdigit():
            qs = qs.filter(tasks__decoration_method_id=int(self.method))
        if self.assignee.isdigit():
            qs = qs.filter(tasks__assigned_to_id=int(self.assignee))
        if self.customer:
            qs = qs.filter(
                Q(customer__display_name__icontains=self.customer)
                | Q(customer__company_name__icontains=self.customer)
            )
        if self.payment in PaymentStatus.values:
            qs = qs.filter(payment_status=self.payment)
        if self.due == "none":
            qs = qs.filter(due_date__isnull=True)
        elif self.due == "this_week":
            qs = qs.filter(due_date__gte=flagger.on, due_date__lte=flagger.on + timedelta(days=7))
        elif self.due in (OVERDUE, AT_RISK):
            # Flags depend on working days, so narrow in the database, then check each job.
            qs = qs.filter(due_date__isnull=False)
            return [j for j in qs.distinct() if flagger(j) == self.due]
        return qs.distinct()


def board(
    filters: Filters,
    *,
    show_side: bool,
    flagger: Flagger | None = None,
    only: list[str] | None = None,
):
    """[(lane name, [(status, label, [Card])])]. `only` limits it to those lanes."""
    flagger = flagger or Flagger()
    lanes = [lane for lane in LANES if only is None or lane[0] in only]
    if show_side:
        lanes.append(SIDE_LANE)
    statuses = [s for _, column in lanes for s in column]
    qs = base_queryset().filter(status__in=statuses)
    cutoff = flagger.on - timedelta(days=CANCELLED_SHOWN_DAYS)
    qs = qs.exclude(status=Status.CANCELLED, status_changed_at__date__lt=cutoff)
    jobs = filters.apply(_ordered(qs), flagger)
    if not isinstance(jobs, list):
        jobs = list(jobs)
    by_status: dict[str, list[Card]] = {s: [] for s in statuses}
    for job in jobs:
        by_status[job.status].append(card(job, flagger))
    return [(name, [(s, Status(s).label, by_status[s]) for s in column]) for name, column in lanes]


# --- Home page --------------------------------------------------------------------

HOME_PANELS = [
    ("Quotes", [Status.QUOTING, Status.QUOTE_SENT]),
    (
        "Mock-ups",
        [Status.MOCKUP_SENT, Status.MOCKUP_APPROVED],
    ),
    ("Invoices", [Status.INVOICED]),
]


@dataclass
class Panel:
    title: str
    graphnix: list[Card]
    customer: list[Card]

    @property
    def count(self) -> int:
        return len(self.graphnix) + len(self.customer)


def _split(title: str, cards: list[Card]) -> Panel:
    return Panel(
        title,
        graphnix=[c for c in cards if c.job.waiting_on == WaitingOn.GRAPHNIX],
        customer=[c for c in cards if c.job.waiting_on == WaitingOn.CUSTOMER],
    )


def home(flagger: Flagger | None = None) -> list[Panel]:
    """Quotes, Mock-ups, Invoices and Almost due, each split by who has to act next.
    Oldest first: the job that has waited longest at its stage leads."""
    flagger = flagger or Flagger()
    panels = []
    open_jobs = base_queryset().exclude(
        status__in=[Status.SHIPPED, Status.CLOSED, Status.CANCELLED]
    )
    for title, statuses in HOME_PANELS:
        condition = Q(status__in=statuses)
        if title == "Mock-ups":
            # A hold for a mock-up change belongs here; other holds stay off the Home page.
            condition |= Q(status=Status.ON_HOLD, hold_reason_code=HoldReason.MOCKUP_CHANGE)
        qs = open_jobs.filter(condition).order_by("status_changed_at")
        panels.append(_split(title, [card(j, flagger) for j in qs]))
    due_soon = [
        c
        for c in (card(j, flagger) for j in open_jobs.filter(due_date__isnull=False))
        if c.flag in (OVERDUE, AT_RISK)
    ]
    due_soon.sort(key=lambda c: (c.job.due_date, c.job.priority))
    panels.append(_split("Almost Due", due_soon))
    return panels


FOLLOW_UP_SETTING = "quote_follow_up_days"
FOLLOW_UP_DAYS = 7


def follow_up_days() -> int:
    from apps.accounts.models import get_setting

    return int(get_setting(FOLLOW_UP_SETTING, FOLLOW_UP_DAYS))


def quote_follow_ups(flagger: Flagger | None = None) -> list[Card]:
    """Quotes sent at least `follow_up_days` ago with no answer yet: someone should call."""
    flagger = flagger or Flagger()
    cutoff = timezone.now() - timedelta(days=follow_up_days())
    qs = base_queryset().filter(status=Status.QUOTE_SENT, quote_sent_at__lte=cutoff)
    return [card(j, flagger) for j in qs.order_by("quote_sent_at")]


def money_summary() -> dict:
    """For the Home page (FR-05): what open jobs still owe, and jobs paid but not started."""
    open_jobs = Job.objects.exclude(status__in=[Status.CLOSED, Status.CANCELLED])
    owing = [
        j.balance_cents
        for j in open_jobs.filter(amount_total_cents__isnull=False).exclude(
            payment_status__in=[PaymentStatus.CANCELLED, PaymentStatus.REFUNDED]
        )
        if j.balance_cents and j.balance_cents > 0
    ]
    not_started = open_jobs.filter(
        payment_status=PaymentStatus.PAID,
        status__in=[Status.QUOTING, Status.QUOTE_SENT, Status.INVOICED],
    )
    return {
        "owing_cents": sum(owing),
        "owing_jobs": len(owing),
        "paid_not_started": not_started.count(),
    }


# --- Search -----------------------------------------------------------------------


def search(query: str, *, can_see_contacts: bool, limit: int = 50):
    """Jobs by Square or internal number, customer name or company, and (for people who may
    see contacts) customer email. Invoice and PO numbers join in Steps 2 and 3."""
    q = query.strip()
    if not q:
        return Job.objects.none()
    number = q.upper()
    terms = (
        Q(square_number__iexact=q)
        | Q(job_number__iexact=number)
        | Q(job_number__iexact=f"G-{number}")
        | Q(title__icontains=q)
        | Q(customer__display_name__icontains=q)
        | Q(customer__company_name__icontains=q)
    )
    if can_see_contacts:
        terms |= Q(customer__email__icontains=q) | Q(contact__email__icontains=q)
    return (
        Job.objects.filter(terms)
        .select_related("customer")
        .order_by(F("closed_at").desc(nulls_first=True), "-created_at")
        .distinct()[:limit]
    )


def exact_number(query: str) -> Job | None:
    """The one job whose number is exactly this, if only one matches (Square's 1042 and the
    internal G-1042 can be different jobs)."""
    q = query.strip()
    if not q:
        return None
    matches = list(
        Job.objects.filter(
            Q(square_number__iexact=q)
            | Q(job_number__iexact=q.upper())
            | Q(job_number__iexact=f"G-{q.upper()}")
        )[:2]
    )
    return matches[0] if len(matches) == 1 else None
