from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

admin.site.site_header = "Graphnix tracker admin"
admin.site.site_title = "Graphnix tracker"

urlpatterns = [
    path("admin/", admin.site.urls),
    # Password sign-in until Microsoft 365 sign-in and the shop-floor PIN (Step 1, week 4).
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("customers/", include("apps.customers.urls")),
    path("inquiries/", include("apps.sales.urls")),
    path("jobs/", include("apps.jobs.urls")),
    path("", include("apps.core.urls")),
]
