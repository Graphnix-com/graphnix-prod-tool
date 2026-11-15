from django.contrib import admin
from django.forms.models import model_to_dict

from .models import AuditLog
from .services import diff, record


class AuditedAdmin(admin.ModelAdmin):
    """Writes an audit entry for every add, change and delete made in the admin."""

    def save_model(self, request, obj, form, change):
        before = model_to_dict(form.instance.__class__.objects.get(pk=obj.pk)) if change else {}
        super().save_model(request, obj, form, change)
        after = model_to_dict(obj)
        changes = diff(before, after) if change else {k: [None, v] for k, v in after.items()}
        if changes or not change:
            record(
                request.user,
                obj,
                "update" if change else "create",
                changes,
                ip=request.META.get("REMOTE_ADDR"),
            )

    def save_formset(self, request, form, formset, change):
        """Inline rows (a customer's contacts and addresses) are audited too."""
        ip = request.META.get("REMOTE_ADDR")
        job = form.instance if form.instance._meta.label_lower == "jobs.job" else None
        edits = [
            (f.instance, {k: [f.initial.get(k), f.cleaned_data.get(k)] for k in f.changed_data})
            for f in formset.forms
            if f.instance.pk and f.has_changed() and f not in formset.deleted_forms
        ]
        removed = [f.instance for f in formset.deleted_forms if f.instance.pk]
        for obj in removed:
            record(request.user, obj, "delete", ip=ip, job=job)
        super().save_formset(request, form, formset, change)
        for obj, changes in edits:
            record(request.user, obj, "update", changes, ip=ip, job=job)
        for obj in formset.new_objects:
            record(request.user, obj, "create", ip=ip, job=job)

    def delete_model(self, request, obj):
        record(request.user, obj, "delete", ip=request.META.get("REMOTE_ADDR"))
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            record(request.user, obj, "delete", ip=request.META.get("REMOTE_ADDR"))
        super().delete_queryset(request, queryset)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("at", "actor", "entity_type", "entity_id", "action")
    list_filter = ("entity_type", "action")
    search_fields = ("entity_type", "entity_id")
    date_hierarchy = "at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
