"""The shared shop-floor PC (NFR-04, decided 7 Nov 2026).

An Admin registers the PC's browser once; it then shows a list of names. Each person taps
their name and enters a 4-digit PIN, works under their own name, and the screen returns to the
name list after 10 minutes without a click or key. A PIN works only in a registered browser,
and 5 wrong tries lock it until an Admin sets a new one.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.db import transaction
from django.utils import timezone

from apps.audit.services import record

from .models import PIN_LENGTH, PIN_MAX_TRIES, ShopFloorDevice

LOCKED = f"This PIN is locked after {PIN_MAX_TRIES} wrong tries. An admin can reset it."

COOKIE = "gx_shop_floor"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 5  # until an Admin switches the PC off in the admin
SESSION_DEVICE = "shop_floor_device"
SESSION_SEEN = "shop_floor_seen"
BACKEND = "django.contrib.auth.backends.ModelBackend"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def device_for(request) -> ShopFloorDevice | None:
    """The registered shop-floor PC this request comes from, if any."""
    if not hasattr(request, "_shop_floor_device"):
        token = request.COOKIES.get(COOKIE)
        request._shop_floor_device = (
            ShopFloorDevice.objects.filter(token_hash=_hash(token), is_active=True).first()
            if token
            else None
        )
    return request._shop_floor_device


def register(request, response, name: str) -> ShopFloorDevice:
    """Make this browser a shop-floor PC: store the device and set its cookie on `response`."""
    token = secrets.token_urlsafe(32)
    device = ShopFloorDevice.objects.create(
        name=name or "Shop-floor PC", token_hash=_hash(token), created_by=request.user
    )
    record(request.user, device, "create", {"name": [None, device.name]}, ip=_ip(request))
    response.set_cookie(
        COOKIE,
        token,
        max_age=COOKIE_MAX_AGE,
        secure=not settings.DEBUG,
        httponly=True,
        samesite="Lax",
    )
    return device


def is_shop_floor_session(request) -> bool:
    return SESSION_DEVICE in request.session


def people():
    """Everyone who can sign in with a PIN, for the name list."""
    return get_user_model().objects.filter(is_active=True).exclude(pin_hash="")


@dataclass
class PinResult:
    ok: bool
    message: str = ""
    must_change: bool = False  # signed in with a temporary PIN: choose a new one first


@transaction.atomic
def try_pin(request, device: ShopFloorDevice, user_id: int, pin: str) -> PinResult:
    """Check a PIN typed on the shop-floor PC and sign the person in if it's right."""
    User = get_user_model()
    user = User.objects.select_for_update().filter(pk=user_id).first()
    if user is None or not user.is_active or not user.has_pin:
        return PinResult(False, "That name can't sign in here. Contact an admin.")
    if user.pin_locked:
        return PinResult(False, LOCKED)
    if not (len(pin) == PIN_LENGTH and pin.isdigit()):
        return PinResult(False, f"Enter your {PIN_LENGTH}-digit PIN.")
    if not user.check_pin(pin):
        user.pin_failed_attempts += 1
        if user.pin_failed_attempts >= PIN_MAX_TRIES:
            user.pin_locked_at = timezone.now()
        user.save(update_fields=["pin_failed_attempts", "pin_locked_at"])
        if user.pin_locked:
            record(None, user, "pin_locked", {"device": [None, device.name]}, ip=_ip(request))
            return PinResult(False, f"Wrong PIN. {LOCKED}")
        left = PIN_MAX_TRIES - user.pin_failed_attempts
        return PinResult(False, f"Wrong PIN. {left} {'try' if left == 1 else 'tries'} left.")

    if user.pin_failed_attempts:
        user.pin_failed_attempts = 0
        user.save(update_fields=["pin_failed_attempts"])
    login(request, user, backend=BACKEND)
    request.session[SESSION_DEVICE] = device.pk
    request.session[SESSION_SEEN] = time.time()
    # The server forgets the session too if the browser sits idle (the page also sends
    # itself back to the name list).
    request.session.set_expiry(settings.SHOP_FLOOR_IDLE_SECONDS)
    ShopFloorDevice.objects.filter(pk=device.pk).update(last_used_at=timezone.now())
    record(
        user,
        user,
        "sign_in",
        {"method": [None, "pin"], "device": [None, device.name]},
        ip=_ip(request),
    )
    return PinResult(True, must_change=user.pin_change_required)


@transaction.atomic
def change_pin(request, new_pin: str) -> None:
    """The signed-in person replaces their temporary PIN with one they chose."""
    user = get_user_model().objects.select_for_update().get(pk=request.user.pk)
    user.set_pin(new_pin)
    user.save(
        update_fields=["pin_hash", "pin_failed_attempts", "pin_locked_at", "pin_change_required"]
    )
    request.user.pin_change_required = False
    record(user, user, "pin_changed", ip=_ip(request))  # never the PIN itself


def _ip(request):
    return request.META.get("REMOTE_ADDR")
