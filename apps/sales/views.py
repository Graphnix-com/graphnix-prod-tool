from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.audit.services import record
from apps.customers.models import Customer
from apps.customers.services import customer_from_inquiry, set_inquiry_customer
from apps.jobs import services as job_services
from apps.jobs.services import InvalidMove

from .forms import ConvertForm, InquiryForm
from .models import Inquiry

OPEN = [Inquiry.Status.NEW, Inquiry.Status.IN_REVIEW]


@login_required
@permission_required("sales.view_inquiry", raise_exception=True)
def inquiry_list(request):
    show_all = request.GET.get("show") == "all"
    inquiries = Inquiry.objects.select_related("customer", "job")
    if not show_all:
        inquiries = inquiries.filter(status__in=OPEN)
    else:
        inquiries = inquiries.order_by("-received_at")[:200]
    return render(
        request, "sales/inquiry_list.html", {"inquiries": inquiries, "show_all": show_all}
    )


@login_required
@permission_required("sales.add_inquiry", raise_exception=True)
def inquiry_new(request):
    form = InquiryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        inquiry = form.save()
        record(request.user, inquiry, "create", {"channel": [None, inquiry.channel]})
        messages.success(request, "Request logged.")
        return redirect("sales:detail", pk=inquiry.pk)
    return render(request, "sales/inquiry_form.html", {"form": form})


def _matches(inquiry, query: str):
    """Customers who might be this inquiry's, for "use this customer"."""
    terms = Q()
    if query:
        terms = (
            Q(display_name__icontains=query)
            | Q(company_name__icontains=query)
            | Q(email__icontains=query)
            | Q(phone__icontains=query)
        )
    else:
        if inquiry.from_email:
            terms |= Q(email__iexact=inquiry.from_email)
        if inquiry.from_phone:
            terms |= Q(phone=inquiry.from_phone)
        if inquiry.from_name:
            terms |= Q(display_name__icontains=inquiry.from_name)
    if not terms:
        return Customer.objects.none()
    return Customer.objects.filter(terms)[:20]


@login_required
@permission_required("sales.view_inquiry", raise_exception=True)
def inquiry_detail(request, pk):
    inquiry = get_object_or_404(Inquiry.objects.select_related("customer"), pk=pk)
    query = request.GET.get("q", "").strip()
    convert_form = ConvertForm(
        initial={"start": "quoting", "title": inquiry.subject or ""},
    )
    return render(
        request,
        "sales/inquiry_detail.html",
        {
            "inquiry": inquiry,
            "query": query,
            "matches": _matches(inquiry, query) if inquiry.status in OPEN else [],
            "convert_form": convert_form,
            "can_convert": request.user.has_perm("jobs.add_job"),
            "is_open": inquiry.status in OPEN,
        },
    )


@login_required
@permission_required("sales.change_inquiry", raise_exception=True)
@require_POST
def inquiry_customer(request, pk):
    inquiry = get_object_or_404(Inquiry, pk=pk)
    try:
        if request.POST.get("customer") == "new":
            customer = customer_from_inquiry(inquiry, user=request.user)
            messages.success(request, f"Customer {customer} created. Check their details.")
        else:
            customer = get_object_or_404(Customer, pk=request.POST.get("customer"))
            set_inquiry_customer(inquiry, customer, user=request.user)
            messages.success(request, f"Linked to {customer}.")
    except PermissionDenied as exc:
        messages.error(request, str(exc))
    return redirect("sales:detail", pk=inquiry.pk)


@login_required
@permission_required("sales.change_inquiry", raise_exception=True)
@require_POST
def inquiry_decline(request, pk):
    inquiry = get_object_or_404(Inquiry, pk=pk)
    if inquiry.status in OPEN:
        old = inquiry.status
        inquiry.status = Inquiry.Status.DECLINED
        inquiry.save(update_fields=["status"])
        record(request.user, inquiry, "update", {"status": [old, inquiry.status]})
        messages.success(request, "Request declined.")
    return redirect("sales:list")


@login_required
@permission_required("jobs.add_job", raise_exception=True)
@require_POST
def inquiry_convert(request, pk):
    inquiry = get_object_or_404(Inquiry, pk=pk)
    form = ConvertForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Pick Quote first or Straight to invoice, and give a title.")
        return redirect("sales:detail", pk=inquiry.pk)
    try:
        job = job_services.convert_inquiry(
            inquiry,
            user=request.user,
            start=form.cleaned_data["start"],
            title=form.cleaned_data["title"],
            square_number=form.cleaned_data["square_number"],
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("sales:detail", pk=inquiry.pk)
    except InvalidMove as exc:
        messages.error(request, str(exc))
        return redirect("sales:detail", pk=inquiry.pk)
    messages.success(request, f"Job {job.display_number} created at {job.get_status_display()}.")
    return redirect("jobs:detail", pk=job.pk)
