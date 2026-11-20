"""What the 5-minute mailbox checks do when reading the mailbox fails.

Microsoft 365 not answering for a few minutes is normal and fixes itself, so it is logged as
a warning (no crash email) and the next check tries again. If it is still failing after
ALERT_AFTER, one crash email goes out. Any other failure (a refused permission, a bad
secret) needs someone to act, so it is emailed straight away."""

from __future__ import annotations

from datetime import timedelta

from django.utils.dateparse import parse_datetime

from .graph import GraphUnavailable

ALERT_AFTER = timedelta(minutes=30)


def failed(result: dict, last: dict, exc: Exception, *, log, what: str, now) -> None:
    """Note in `result` that reading failed with `exc`, carrying on from `last` (the
    previous check's result), and log it at the right level."""
    result["error"] = str(exc)[:500]
    still_failing = bool(last.get("error"))
    since = (last.get("failing_since") if still_failing else "") or result["at"]
    result["failing_since"] = since
    alerted = still_failing and bool(last.get("alerted"))
    if not isinstance(exc, GraphUnavailable):
        log.error("Reading %s failed: %s", what, exc)
        result["alerted"] = True
    elif not alerted and now - parse_datetime(since) >= ALERT_AFTER:
        log.error("Reading %s has failed for over 30 minutes: %s", what, exc)
        result["alerted"] = True
    else:
        log.warning("Reading %s failed; trying again in 5 minutes: %s", what, exc)
        result["alerted"] = alerted


def is_brief_outage(result: dict, now) -> bool:
    """For the health panel: a Microsoft 365 blip that hasn't lasted ALERT_AFTER yet."""
    since = parse_datetime(result.get("failing_since") or "")
    return bool(since) and not result.get("alerted") and now - since < ALERT_AFTER
