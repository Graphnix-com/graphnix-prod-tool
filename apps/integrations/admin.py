"""Read-only views of what Square sent, for Admin (and Manager, invoices only)."""

from django.contrib import admin

from .models import SquareInvoice, SquareOrder, WebhookEvent


class ReadOnly(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SquareInvoice)
class SquareInvoiceAdmin(ReadOnly):
    list_display = (
        "invoice_number",
        "title",
        "recipient_name",
        "status",
        "job",
        "square_updated_at",
    )
    list_filter = ("status", "match_method")
    search_fields = ("invoice_number", "title", "recipient_name", "square_id")
    list_select_related = ("job",)


@admin.register(WebhookEvent)
class WebhookEventAdmin(ReadOnly):
    list_display = ("event_type", "square_object_id", "received_at", "processed_at", "attempts")
    list_filter = ("event_type",)
    search_fields = ("event_id", "square_object_id")
    readonly_fields = ("last_error",)


@admin.register(SquareOrder)
class SquareOrderAdmin(ReadOnly):
    list_display = ("number", "buyer_name", "source_name", "state", "total_cents", "job")
    list_filter = ("state", "source_name")
    search_fields = ("square_id", "reference", "buyer_name")
