"""Read-mostly admin for jobs. Status changes and new jobs go through `services` only
(the job screens arrive in week 2), so the admin can't add jobs or edit their status."""

from django.contrib import admin

from apps.audit.admin import AuditedAdmin

from .models import Job, JobNote, JobStatusHistory


class HistoryInline(admin.TabularInline):
    model = JobStatusHistory
    extra = 0
    can_delete = False
    fields = ("changed_at", "from_status", "to_status", "changed_by", "source", "note")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class NoteInline(admin.TabularInline):
    model = JobNote
    extra = 0
    fields = ("created_at", "author", "body", "is_internal")
    readonly_fields = ("created_at", "author")


@admin.register(Job)
class JobAdmin(AuditedAdmin):
    list_display = ("job_number", "square_number", "title", "customer", "status", "due_date")
    list_filter = ("status", "priority", "source")
    search_fields = ("job_number", "square_number", "title", "customer__display_name")
    autocomplete_fields = ("customer",)
    readonly_fields = (
        "job_number",
        "status",
        "status_changed_at",
        "resume_status",
        "suggested_due_date",
        "mockup_approved_at",
        "hold_reason_code",
        "hold_reason",
        "cancel_reason",
        "created_by",
        "closed_at",
    )
    inlines = [HistoryInline, NoteInline]

    def has_add_permission(self, request):
        return False

    def save_formset(self, request, form, formset, change):
        for f in formset.forms:
            if formset.model is JobNote and f.instance.pk is None and f.has_changed():
                f.instance.author = request.user
        super().save_formset(request, form, formset, change)
