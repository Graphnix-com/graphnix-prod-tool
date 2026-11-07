from django.urls import path

from . import views

app_name = "sales"

urlpatterns = [
    path("", views.inquiry_list, name="list"),
    path("new/", views.inquiry_new, name="new"),
    path("<int:pk>/", views.inquiry_detail, name="detail"),
    path("<int:pk>/customer/", views.inquiry_customer, name="customer"),
    path("<int:pk>/decline/", views.inquiry_decline, name="decline"),
    path("<int:pk>/convert/", views.inquiry_convert, name="convert"),
]
