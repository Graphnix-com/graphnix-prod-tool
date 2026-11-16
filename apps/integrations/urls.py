from django.urls import path

from . import views

app_name = "integrations"

urlpatterns = [
    path("square/webhook/", views.square_webhook, name="square_webhook"),
]
