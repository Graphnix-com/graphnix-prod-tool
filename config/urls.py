from django.contrib import admin
from django.shortcuts import redirect
from django.urls import include, path, re_path

from apps.accounts import views as account_views
from apps.jobs import views as job_views

admin.site.site_header = "GRAPHNIX ADMIN"
admin.site.site_title = "GRAPHNIX tracker"

urlpatterns = [
    path("admin/login/", account_views.admin_login),
    path("admin/", admin.site.urls),
    path("", include("apps.accounts.urls")),
    path("customers/", include("apps.customers.urls")),
    path("requests/", include("apps.sales.urls")),
    # Old bookmarks: /inquiries/... now lives at /requests/...
    re_path(
        r"^inquiries/(?P<rest>.*)$",
        lambda request, rest: redirect(f"/requests/{rest}", permanent=True),
    ),
    path("jobs/", include("apps.jobs.urls")),
    path("integrations/", include("apps.integrations.urls")),
    path("", job_views.home, name="home"),
    path("", include("apps.core.urls")),
]
