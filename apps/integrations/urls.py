from django.urls import path

from . import views

app_name = "integrations"

urlpatterns = [
    path("square/webhook/", views.square_webhook, name="square_webhook"),
    path("invoices/", views.inbox, name="inbox"),
    path("invoices/<int:pk>/link/", views.invoice_link, name="invoice_link"),
    path("invoices/<int:pk>/unlink/", views.invoice_unlink, name="invoice_unlink"),
    path("invoices/<int:pk>/not-a-job/", views.invoice_set_aside, name="invoice_set_aside"),
    path("payment-gates/", views.payment_gates, name="payment_gates"),
]
