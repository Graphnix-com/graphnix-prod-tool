from django.contrib import admin

from apps.audit.admin import AuditedAdmin

from .models import Inquiry


@admin.register(Inquiry)
class InquiryAdmin(AuditedAdmin):
    list_display = ("received_at", "channel", "customer", "from_name", "subject", "status")
    list_filter = ("status", "channel")
    search_fields = ("from_name", "from_email", "subject", "customer__display_name")
    autocomplete_fields = ("customer",)
