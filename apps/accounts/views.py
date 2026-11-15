from django import forms
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import entra, shopfloor

SESSION_NEXT = "entra_next"


def _safe_next(request, value: str) -> str:
    if value and url_has_allowed_host_and_scheme(
        value, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return value
    return reverse("home")


class LoginView(auth_views.LoginView):
    """Microsoft 365 for office staff; a password for the fallback admin account (and Avery
    away from the shop floor). On the shop-floor PC this goes to the name list instead."""

    def dispatch(self, request, *args, **kwargs):
        if shopfloor.device_for(request) and "office" not in request.GET:
            return redirect("shop_floor")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["microsoft_enabled"] = entra.enabled()
        context["show_password"] = (
            not entra.enabled() or "password" in self.request.GET or (self.request.method == "POST")
        )
        return context


def microsoft_start(request):
    if not entra.enabled():
        messages.error(request, "Microsoft 365 sign-in isn't set up here yet.")
        return redirect("login")
    request.session[SESSION_NEXT] = _safe_next(request, request.GET.get("next", ""))
    redirect_uri = request.build_absolute_uri(reverse("microsoft_callback"))
    return redirect(entra.start(request, redirect_uri))


def microsoft_callback(request):
    if not entra.enabled():
        return redirect("login")
    next_url = request.session.pop(SESSION_NEXT, reverse("home"))
    ip = request.META.get("REMOTE_ADDR")
    try:
        user = entra.user_for_claims(entra.finish(request), ip=ip)
    except entra.SignInRefused as exc:
        messages.error(request, str(exc))
        return redirect("login")
    login(request, user, backend=shopfloor.BACKEND)
    return redirect(next_url)


def admin_login(request):
    """The admin's own sign-in page only takes passwords; send people to the real one."""
    if request.user.is_authenticated:
        raise PermissionDenied("The admin pages are for Admin/Owner.")
    query = request.GET.urlencode()
    return redirect(f"{reverse('login')}{'?' + query if query else ''}")


@require_POST
def logout_view(request):
    logout(request)
    return redirect("shop_floor" if shopfloor.device_for(request) else "login")


# --- Shop-floor PC ----------------------------------------------------------------


def _not_this_pc(request):
    return render(request, "accounts/shop_floor_not_set_up.html", status=403)


def shop_floor(request):
    """The name list. Opening it signs out whoever was using the PC."""
    device = shopfloor.device_for(request)
    if device is None:
        return _not_this_pc(request)
    if request.user.is_authenticated:
        logout(request)
    return render(request, "accounts/shop_floor.html", {"people": shopfloor.people()})


def shop_floor_pin(request, pk):
    device = shopfloor.device_for(request)
    if device is None:
        return _not_this_pc(request)
    person = get_object_or_404(shopfloor.people(), pk=pk)
    error = ""
    if request.method == "POST":
        result = shopfloor.try_pin(request, device, person.pk, request.POST.get("pin", "").strip())
        if result.ok:
            return redirect("home")
        error = result.message
        person.refresh_from_db()
    status = 200 if not error else 400
    return render(
        request, "accounts/shop_floor_pin.html", {"person": person, "error": error}, status=status
    )


class DeviceForm(forms.Form):
    name = forms.CharField(max_length=100, initial="Shop-floor PC", label="Name for this PC")


@login_required
@permission_required("accounts.add_shopfloordevice", raise_exception=True)
def shop_floor_setup(request):
    """An Admin, signed in on the shop-floor PC, makes this browser the shop-floor PC."""
    form = DeviceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        response = redirect("shop_floor")
        shopfloor.register(request, response, form.cleaned_data["name"])
        # The PC now belongs to the shop floor, not to the Admin who set it up.
        logout(request)
        return response
    return render(
        request,
        "accounts/shop_floor_setup.html",
        {"form": form, "already": shopfloor.device_for(request)},
    )
