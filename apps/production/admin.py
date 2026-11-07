from django.contrib import admin

from apps.audit.admin import AuditedAdmin

from .models import DecorationMethod, Station


@admin.register(DecorationMethod)
class DecorationMethodAdmin(AuditedAdmin):
    list_display = ("name", "code", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "code")
    prepopulated_fields = {"code": ("name",)}


@admin.register(Station)
class StationAdmin(AuditedAdmin):
    list_display = ("name", "decoration_method", "is_active")
    list_filter = ("decoration_method", "is_active")
