from django.contrib.auth.decorators import login_required, permission_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from . import health


@require_GET
@never_cache
def healthz(request):
    """Used by the Docker health check: is this process up and can it reach the database?"""
    if health.database_ok():
        return JsonResponse({"status": "ok"})
    return JsonResponse({"status": "error", "database": "unreachable"}, status=503)


@login_required
@permission_required("core.view_system_health", raise_exception=True)
@never_cache
def health_panel(request):
    checks = health.all_checks()
    worst = (
        health.FAIL
        if any(c.status == health.FAIL for c in checks)
        else health.WARN
        if any(c.status == health.WARN for c in checks)
        else health.OK
    )
    return render(request, "core/health.html", {"checks": checks, "overall": worst})


def home(request):
    # The job board arrives in Step 1; until then, staff land on the health panel.
    return redirect("core:health")
