"""Ends a shop-floor PC session after 10 idle minutes, or if it shows up in another browser."""

import time

from django.conf import settings
from django.contrib.auth import logout
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse

from . import shopfloor

BACKGROUND_HEADER = "X-Background-Refresh"


def is_background(request) -> bool:
    """The board's once-a-minute refresh: it must not count as someone using the PC."""
    return request.headers.get(BACKGROUND_HEADER) == "1" or request.GET.get("partial") == "1"


class ShopFloorSessionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and shopfloor.is_shop_floor_session(request):
            device = shopfloor.device_for(request)
            idle = time.time() - request.session.get(shopfloor.SESSION_SEEN, 0)
            same_pc = device is not None and device.pk == request.session[shopfloor.SESSION_DEVICE]
            if not same_pc or idle > settings.SHOP_FLOOR_IDLE_SECONDS:
                logout(request)
                if is_background(request):
                    return HttpResponse(status=204)
                return redirect("shop_floor" if device else "login")
            if not is_background(request):
                request.session[shopfloor.SESSION_SEEN] = time.time()
            if request.user.pin_change_required and request.path not in self._pin_paths():
                # Signed in with a PIN an Admin set: nothing else until they choose their own.
                if is_background(request):
                    return HttpResponse(status=204)
                return redirect("shop_floor_change_pin")
        return self.get_response(request)

    @staticmethod
    def _pin_paths() -> set[str]:
        return {reverse("shop_floor_change_pin"), reverse("logout"), reverse("shop_floor")}
