from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from apps.audit.admin import AuditedAdmin

from .models import AppSetting, User


class StaffCreationForm(UserCreationForm):
    class Meta:
        model = User
        fields = ("email", "full_name")


class StaffChangeForm(UserChangeForm):
    class Meta:
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    form = StaffChangeForm
    add_form = StaffCreationForm
    ordering = ("full_name",)
    list_display = ("email", "full_name", "is_active", "is_staff", "last_login")
    list_filter = ("is_active", "is_staff", "is_superuser", "groups")
    search_fields = ("email", "full_name")
    readonly_fields = ("last_login", "date_joined", "entra_object_id")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Person", {"fields": ("full_name", "entra_object_id", "default_station")}),
        (
            "Access",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "full_name", "password1", "password2")}),
    )


@admin.register(AppSetting)
class AppSettingAdmin(AuditedAdmin):
    list_display = ("key", "value", "updated_by", "updated_at")
    readonly_fields = ("updated_by", "updated_at")

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
