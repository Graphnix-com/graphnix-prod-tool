from django.urls import path

from . import views

app_name = "customers"

urlpatterns = [
    path("", views.customer_list, name="list"),
    path("new/", views.customer_new, name="new"),
    path("<int:pk>/", views.customer_detail, name="detail"),
    path("<int:pk>/edit/", views.customer_edit, name="edit"),
]
