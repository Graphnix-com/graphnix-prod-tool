"""Crash emails to the admin (NFR-08), throttled so one fault sends one email."""

import hashlib

from django.conf import settings
from django.core.cache import cache
from django.utils.log import AdminEmailHandler


class ThrottledAdminEmailHandler(AdminEmailHandler):
    """Like Django's AdminEmailHandler, but the same error is emailed at most once
    per CRASH_EMAIL_THROTTLE_SECONDS (10 minutes by default)."""

    def emit(self, record):
        if not settings.ADMINS:
            return
        try:
            exc = record.exc_info[1] if record.exc_info else None
            fingerprint = f"{record.name}|{record.levelno}|{type(exc).__name__}|{record.msg}"
            key = "crash-email:" + hashlib.sha256(fingerprint.encode()).hexdigest()
            if not cache.add(key, 1, timeout=settings.CRASH_EMAIL_THROTTLE_SECONDS):
                return
        except Exception:  # noqa: S110 - cache down (e.g. DB outage): still send the email
            pass
        super().emit(record)
