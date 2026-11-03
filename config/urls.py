from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "Graphnix tracker admin"
admin.site.site_title = "Graphnix tracker"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("apps.core.urls")),
]
