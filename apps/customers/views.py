from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from .forms import AddressFormSet, ContactFormSet, CustomerForm
from .models import Customer
from .services import save_customer


@login_required
@permission_required("customers.view_customer", raise_exception=True)
def customer_list(request):
    query = request.GET.get("q", "").strip()
    customers = Customer.objects.all()
    if query:
        customers = customers.filter(
            Q(display_name__icontains=query)
            | Q(company_name__icontains=query)
            | Q(email__icontains=query)
            | Q(phone__icontains=query)
        )
    return render(
        request,
        "customers/customer_list.html",
        {"customers": customers[:200], "query": query},
    )


@login_required
@permission_required("customers.view_customer", raise_exception=True)
def customer_detail(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    return render(
        request,
        "customers/customer_detail.html",
        {
            "customer": customer,
            "jobs": customer.jobs.order_by("-created_at"),
            "can_see_contacts": request.user.has_perm("customers.view_contact_details"),
            "can_edit": request.user.has_perm("customers.change_customer"),
        },
    )


def _customer_form(request, customer):
    form = CustomerForm(request.POST or None, instance=customer, prefix="c")
    contacts = ContactFormSet(request.POST or None, instance=customer, prefix="contacts")
    addresses = AddressFormSet(request.POST or None, instance=customer, prefix="addresses")
    if request.method == "POST" and all(f.is_valid() for f in (form, contacts, addresses)):
        saved = save_customer(user=request.user, form=form, contacts=contacts, addresses=addresses)
        messages.success(request, "Customer saved.")
        return redirect("customers:detail", pk=saved.pk)
    return render(
        request,
        "customers/customer_form.html",
        {"form": form, "contacts": contacts, "addresses": addresses, "customer": customer},
    )


@login_required
@permission_required("customers.add_customer", raise_exception=True)
def customer_new(request):
    return _customer_form(request, Customer())


@login_required
@permission_required("customers.change_customer", raise_exception=True)
def customer_edit(request, pk):
    return _customer_form(request, get_object_or_404(Customer, pk=pk))
