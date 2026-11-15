from django.conf import settings

from . import shopfloor


def sign_in(request):
    return {
        "shop_floor_session": shopfloor.is_shop_floor_session(request)
        if hasattr(request, "session")
        else False,
        "shop_floor_idle_seconds": settings.SHOP_FLOOR_IDLE_SECONDS,
    }
