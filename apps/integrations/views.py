import json
import logging

from django.conf import settings
from django.db import transaction
from django.http import Http404, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import square, tasks
from .models import WebhookEvent

log = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def square_webhook(request):
    """Square's deliveries (INT-01): checked, stored once, then handed to the worker.
    Answers at once so Square doesn't time out and retry."""
    if not settings.SQUARE_WEBHOOK_SIGNATURE_KEY:
        raise Http404
    url = settings.SQUARE_WEBHOOK_URL or request.build_absolute_uri()
    signature = request.headers.get("x-square-hmacsha256-signature", "")
    if not square.valid_signature(request.body, signature, url):
        log.warning("Square webhook refused: bad or missing signature")
        return HttpResponse(status=403)
    try:
        payload = json.loads(request.body)
        event_id = str(payload["event_id"])
        event_type = str(payload.get("type") or "")
    except (ValueError, KeyError, TypeError):
        return HttpResponse(status=400)
    data = payload.get("data") or {}
    with transaction.atomic():
        event, created = WebhookEvent.objects.get_or_create(
            event_id=event_id,
            defaults={
                "event_type": event_type[:60],
                "square_object_id": str(data.get("id") or "")[:100],
                "body": payload,
            },
        )
        if created:
            transaction.on_commit(lambda: tasks.process_webhook_event.defer(event_pk=event.pk))
    return HttpResponse(status=200)
