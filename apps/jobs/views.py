from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.production.models import DecorationMethod, ProductionTask
from apps.sales.models import Inquiry

from . import assignment, board, editing, files, money, services, timeline
from .editing import Locked
from .flags import Flagger
from .forms import ItemForm, JobDetailsForm, NoteForm, TaskForm
from .models import Job, JobFile, JobItem, PaymentStatus
from .services import InvalidMove

TABS = ["items", "timeline", "details"]


def _errors(exc: ValidationError) -> str:
    return " ".join(exc.messages)


def _job_url(job: Job, tab: str = "items") -> str:
    return f"{reverse('jobs:detail', args=[job.pk])}?tab={tab}"


@login_required
def home(request):
    """What needs doing, split by who has to act next. Admin and Manager land here; everyone
    else lands on the board."""
    if not request.user.has_perm("jobs.view_money"):
        if request.user.has_perm("jobs.view_job"):
            return redirect("jobs:list")
        if request.user.has_perm("sales.view_inquiry"):
            return redirect("sales:list")
        return redirect("core:health")
    flagger = Flagger()
    return render(
        request,
        "jobs/home.html",
        {
            "panels": board.home(flagger),
            "today": flagger.on,
            "open_inquiries": Inquiry.objects.filter(
                status__in=[Inquiry.Status.NEW, Inquiry.Status.IN_REVIEW]
            ).count(),
            **board.money_summary(),
            "follow_ups": board.quote_follow_ups(flagger),
            "follow_up_days": board.follow_up_days(),
        },
    )


@login_required
@permission_required("jobs.view_job", raise_exception=True)
def job_list(request):
    """The board (FR-31): a column per stage, in rows by lane, with filters."""
    can_see_money = request.user.has_perm("jobs.view_money")
    filters = board.Filters.from_request(request.GET, can_see_money)
    show_side = request.GET.get("side") == "1"
    flagger = Flagger()
    own_lanes = board.lanes_for(request.user)
    show_all = request.GET.get("all") == "1" or len(own_lanes) == len(board.LANES)
    context = {
        "lanes": board.board(
            filters, show_side=show_side, flagger=flagger, only=None if show_all else own_lanes
        ),
        "show_all": show_all,
        "own_lanes": own_lanes,
        "limited": len(own_lanes) < len(board.LANES),
        "mine": board.assigned_to(request.user, flagger),
        "filters": filters,
        "show_side": show_side,
        "today": flagger.on,
        "can_see_money": can_see_money,
        "methods": DecorationMethod.objects.filter(is_active=True),
        "assignees": get_user_model().objects.filter(is_active=True),
        "due_choices": board.DUE_FILTERS,
        "payment_choices": PaymentStatus.choices,
    }
    template = "jobs/_board.html" if request.GET.get("partial") == "1" else "jobs/board.html"
    return render(request, template, context)


@login_required
@permission_required("jobs.view_job", raise_exception=True)
def job_search(request):
    """Header search (FR-33). An exact job number goes straight to the job."""
    query = request.GET.get("q", "").strip()
    if job := board.exact_number(query):
        return redirect("jobs:detail", pk=job.pk)
    can_see_contacts = request.user.has_perm("customers.view_contact_details")
    jobs = board.search(query, can_see_contacts=can_see_contacts)
    return render(request, "jobs/search.html", {"query": query, "jobs": jobs})


@login_required
@permission_required("jobs.view_job", raise_exception=True)
def job_detail(request, pk):
    job = get_object_or_404(Job.objects.select_related("customer", "contact", "inquiry"), pk=pk)
    tab = request.GET.get("tab") if request.GET.get("tab") in TABS else "items"
    items = job.items.prefetch_related("sizes", "tasks__decoration_method", "tasks__assigned_to")
    assignable = list(assignment.assignable_people(request.user))
    context = {
        "job": job,
        "tab": tab,
        "items": items,
        "editable": editing.items_editable(job) and request.user.has_perm("jobs.change_job"),
        "actions": services.actions_for(job, request.user),
        "note_form": NoteForm(),
        "can_see_money": request.user.has_perm("jobs.view_money"),
        "can_see_contacts": request.user.has_perm("customers.view_contact_details"),
        # Who this person may give tasks to; Production only ever sees themselves.
        "assignable": assignable,
        "assignable_ids": {p.pk for p in assignable},
        "assign_others": any(p.pk != request.user.pk for p in assignable),
        "can_assign": job.status not in assignment.CLOSED,
    }
    if tab == "details":
        context["files"] = list(job.files.select_related("uploaded_by"))
        context["file_types"] = ",".join(f".{e}" for e in sorted(files.ALLOWED))
    if tab == "details" and context["can_see_money"]:
        context["invoices"] = list(job.square_invoices.all())
        context["store_orders"] = list(job.square_orders.all())
        if job.amount_total_cents is not None:
            context["balance"] = job.amount_total_cents - job.amount_paid_cents
    if tab == "timeline":
        context["entries"] = timeline.build(
            job, show_money=request.user.has_perm("jobs.view_money")
        )
    return render(request, "jobs/job_detail.html", context)


@login_required
@permission_required("jobs.change_job", raise_exception=True)
def job_edit(request, pk):
    job = get_object_or_404(Job, pk=pk)
    form = JobDetailsForm(request.POST or None, instance=Job.objects.get(pk=pk))
    if request.method == "POST" and form.is_valid():
        try:
            editing.update_details(job, user=request.user, data=form.cleaned_data)
        except ValidationError as exc:
            form.add_error("square_number", _errors(exc))
        else:
            messages.success(request, "Job details saved.")
            return redirect(_job_url(job, "details"))
    return render(request, "jobs/job_form.html", {"job": job, "form": form})


# --- Status moves ------------------------------------------------------------------


@login_required
@require_POST
def job_action(request, pk):
    job = get_object_or_404(Job, pk=pk)
    action = request.POST.get("action")
    note = request.POST.get("note", "")
    user = request.user
    try:
        if money.has_amount(note):
            raise InvalidMove(money.MESSAGE)
        if action == "move":
            services.move(job, request.POST.get("to_status", ""), user=user, note=note)
        elif action == "skip_mockup":
            services.skip_mockup(job, user=user, note=note)
        elif action == "reopen_mockup":
            services.reopen_mockup(job, user=user, note=note)
        elif action == "hold":
            services.hold(
                job, user=user, reason_code=request.POST.get("reason_code", ""), note=note
            )
        elif action == "release":
            services.release_hold(job, user=user, note=note)
        elif action == "cancel":
            services.cancel(job, user=user, note=note)
        elif action == "reopen":
            services.reopen_cancelled(job, user=user, note=note)
        else:
            raise InvalidMove("Unknown action.")
    except (InvalidMove, PermissionDenied) as exc:
        messages.error(request, str(exc))
    else:
        job.refresh_from_db()
        messages.success(request, f"{job.display_number} is now {job.get_status_display()}.")
    return redirect(_job_url(job, request.POST.get("tab", "items")))


# --- Items and tasks ---------------------------------------------------------------


def _editable_or_bounce(request, job):
    if not editing.items_editable(job):
        messages.error(request, "Items and tasks can't change once the job is in production.")
        return redirect(_job_url(job))
    return None


@login_required
@permission_required("jobs.change_job", raise_exception=True)
def item_form(request, pk, item_pk=None):
    job = get_object_or_404(Job, pk=pk)
    item = get_object_or_404(JobItem, pk=item_pk, job=job) if item_pk else None
    if bounce := _editable_or_bounce(request, job):
        return bounce
    form = ItemForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        try:
            editing.save_item(
                job, user=request.user, data=form.cleaned_data, sizes=form.sizes, item=item
            )
        except (ValidationError, Locked) as exc:
            form.add_error(None, _errors(exc) if isinstance(exc, ValidationError) else str(exc))
        else:
            messages.success(request, "Item saved.")
            return redirect(_job_url(job))
    return render(request, "jobs/item_form.html", {"job": job, "item": item, "form": form})


@login_required
@permission_required("jobs.change_job", raise_exception=True)
@require_POST
def item_delete(request, pk, item_pk):
    item = get_object_or_404(JobItem, pk=item_pk, job_id=pk)
    try:
        editing.delete_item(item, user=request.user)
        messages.success(request, "Item removed.")
    except Locked as exc:
        messages.error(request, str(exc))
    return redirect(_job_url(item.job))


@login_required
@permission_required("jobs.change_job", raise_exception=True)
def task_form(request, pk, task_pk=None):
    job = get_object_or_404(Job, pk=pk)
    task = get_object_or_404(ProductionTask, pk=task_pk, job=job) if task_pk else None
    if bounce := _editable_or_bounce(request, job):
        return bounce
    can_add = request.user.has_perm("production.add_decorationmethod")
    initial = {"job_item": request.GET.get("item")} if not task else None
    form = TaskForm(
        request.POST or None, instance=task, job=job, can_add_method=can_add, initial=initial
    )
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        try:
            if data.get("new_method"):
                data["decoration_method"] = editing.add_method(
                    user=request.user, name=data["new_method"]
                )
            editing.save_task(job, user=request.user, data=data, task=task)
        except (ValidationError, Locked) as exc:
            form.add_error(None, _errors(exc) if isinstance(exc, ValidationError) else str(exc))
        else:
            messages.success(request, "Task saved.")
            return redirect(_job_url(job))
    return render(request, "jobs/task_form.html", {"job": job, "task": task, "form": form})


@login_required
@permission_required("jobs.change_job", raise_exception=True)
@require_POST
def task_delete(request, pk, task_pk):
    task = get_object_or_404(ProductionTask, pk=task_pk, job_id=pk)
    try:
        editing.delete_task(task, user=request.user)
        messages.success(request, "Task removed.")
    except Locked as exc:
        messages.error(request, str(exc))
    return redirect(_job_url(task.job))


@login_required
@permission_required("jobs.view_job", raise_exception=True)
@require_POST
def task_assign(request, pk, task_pk):
    """Give a task to someone, take it, or let it go (see jobs.assignment for who may)."""
    job = get_object_or_404(Job, pk=pk)
    task = get_object_or_404(ProductionTask, pk=task_pk, job=job)
    value = request.POST.get("assignee", "")
    if value == "me":
        assignee = request.user
    elif value.isdigit():
        assignee = get_object_or_404(get_user_model(), pk=int(value))
    else:
        assignee = None
    try:
        task = assignment.assign(task, assignee, user=request.user)
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
    else:
        who = task.assigned_to.get_short_name() if task.assigned_to else "nobody"
        messages.success(request, f"{task.decoration_method} is now with {who}.")
    return redirect(_job_url(job))


@login_required
@permission_required("jobs.add_jobnote", raise_exception=True)
@require_POST
def note_add(request, pk):
    job = get_object_or_404(Job, pk=pk)
    form = NoteForm(request.POST)
    if form.is_valid():
        editing.add_note(job, user=request.user, body=form.cleaned_data["body"])
        messages.success(request, "Note added.")
    else:
        messages.error(request, " ".join(form.errors.get("body", ["The note is empty."])))
    return redirect(_job_url(job, "timeline"))


# --- Files -------------------------------------------------------------------------


@login_required
@require_POST
@permission_required("jobs.view_job", raise_exception=True)
def file_add(request, pk):
    job = get_object_or_404(Job, pk=pk)
    try:
        saved = files.add(job, request.FILES.getlist("files"), user=request.user)
    except (ValidationError, PermissionDenied) as exc:
        messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
    else:
        messages.success(request, f"Added {len(saved)} file{'s' if len(saved) != 1 else ''}.")
    return redirect(_job_url(job, "details"))


@login_required
@permission_required("jobs.view_job", raise_exception=True)
def file_download(request, pk, file_pk):
    """Through the tracker only. Photos open in the browser; everything else (SVG can carry
    script, AI and EPS need a design app) downloads."""
    f = get_object_or_404(JobFile, pk=file_pk, job_id=pk)
    try:
        handle = f.file.open("rb")
    except FileNotFoundError as exc:
        raise Http404 from exc
    response = FileResponse(handle, as_attachment=not f.is_image, filename=f.name)
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; sandbox"
    response["Cache-Control"] = "private, max-age=3600"
    return response


@login_required
@require_POST
def file_delete(request, pk, file_pk):
    f = get_object_or_404(JobFile, pk=file_pk, job_id=pk)
    try:
        files.remove(f, user=request.user)
    except PermissionDenied as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Removed {f.name}.")
    return redirect(_job_url(f.job, "details"))
