from django.contrib import admin
from django.urls import include, path

from apps.accounts import views as account_views
from apps.jobs import views as job_views

admin.site.site_header = "Graphnix tracker admin"
admin.site.site_title = "Graphnix tracker"

urlpatterns = [
    path("admin/login/", account_views.admin_login),
    path("admin/", admin.site.urls),
    path("", include("apps.accounts.urls")),
    path("customers/", include("apps.customers.urls")),
    path("inquiries/", include("apps.sales.urls")),
    path("jobs/", include("apps.jobs.urls")),
    path("", job_views.home, name="home"),
    path("", include("apps.core.urls")),
]
