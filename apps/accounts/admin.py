from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm
from django.utils import timezone

from apps.audit.admin import AuditedAdmin
from apps.audit.services import record

from .models import PIN_LENGTH, AppSetting, ShopFloorDevice, User


def _pin_field():
    return forms.CharField(
        label="New PIN",
        required=False,
        min_length=PIN_LENGTH,
        max_length=PIN_LENGTH,
        widget=forms.PasswordInput(attrs={"inputmode": "numeric", "autocomplete": "new-password"}),
        help_text=f"{PIN_LENGTH} digits, for the shop-floor PC. Setting one also unlocks it.",
    )


def _clean_pin(form):
    pin = form.cleaned_data.get("new_pin", "")
    if pin and not pin.isdigit():
        raise forms.ValidationError(f"A PIN is {PIN_LENGTH} digits.")
    return pin


class StaffCreationForm(AdminUserCreationForm):
    """Office staff get an email (for Microsoft 365). Production staff get no email and no
    password, just a PIN."""

    new_pin = _pin_field()

    class Meta:
        model = User
        fields = ("email", "full_name")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].required = False
        self.fields[
            "email"
        ].help_text = "Their Microsoft 365 address. Leave empty for shop-floor staff."
        self.fields["usable_password"].initial = "false"

    clean_new_pin = _clean_pin

    def clean_email(self):
        return self.cleaned_data.get("email") or None

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("set_usable_password") and not cleaned.get("email"):
            self.add_error("email", "Signing in with a password needs an email address.")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        if self.cleaned_data.get("new_pin"):
            user.set_pin(self.cleaned_data["new_pin"])
        if commit:
            user.save()
            self.save_m2m()
        return user


class StaffChangeForm(UserChangeForm):
    new_pin = _pin_field()
    remove_pin = forms.BooleanField(
        label="Remove PIN", required=False, help_text="They can no longer sign in on the PC."
    )

    class Meta:
        model = User
        fields = "__all__"

    clean_new_pin = _clean_pin

    def clean_email(self):
        return self.cleaned_data.get("email") or None


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    form = StaffChangeForm
    add_form = StaffCreationForm
    ordering = ("full_name",)
    list_display = ("full_name", "email", "is_active", "is_staff", "pin_status", "last_login")
    list_filter = ("is_active", "is_staff", "is_superuser", "groups")
    search_fields = ("email", "full_name")
    readonly_fields = ("last_login", "date_joined", "entra_object_id", "pin_status")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Person", {"fields": ("full_name", "entra_object_id", "default_station")}),
        ("Shop-floor PIN", {"fields": ("pin_status", "new_pin", "remove_pin")}),
        (
            "Access",
            {
                "fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions"),
                "description": (
                    "Admin/Owner, Manager and Production follow the person's Microsoft 365 "
                    "groups at each Microsoft sign-in. QA and shipping and Customer entry are "
                    "set here."
                ),
            },
        ),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "full_name",
                    "email",
                    "new_pin",
                    "usable_password",
                    "password1",
                    "password2",
                ),
            },
        ),
    )

    @admin.display(description="PIN")
    def pin_status(self, obj):
        if not obj.pk or not obj.has_pin:
            return "None"
        if obj.pin_locked:
            when = timezone.localtime(obj.pin_locked_at).strftime("%b %-d, %-I:%M %p")
            return f"Locked since {when}"
        if obj.pin_failed_attempts:
            return f"Set ({obj.pin_failed_attempts} wrong tries)"
        return "Set"

    def save_model(self, request, obj, form, change):
        ip = request.META.get("REMOTE_ADDR")
        pin_change = None
        if change and form.cleaned_data.get("new_pin"):
            obj.set_pin(form.cleaned_data["new_pin"])
            pin_change = "pin_set"
        elif change and form.cleaned_data.get("remove_pin"):
            obj.clear_pin()
            pin_change = "pin_removed"
        super().save_model(request, obj, form, change)
        if not change:
            record(request.user, obj, "create", {"full_name": [None, obj.full_name]}, ip=ip)
            if obj.has_pin:
                record(request.user, obj, "pin_set", ip=ip)
        elif pin_change:
            record(request.user, obj, pin_change, ip=ip)  # never the PIN itself
        if change:
            skip = {"new_pin", "remove_pin", "password"}
            fields = sorted(f for f in form.changed_data if f not in skip)
            if fields:
                record(request.user, obj, "update", {"fields": [None, fields]}, ip=ip)


@admin.register(ShopFloorDevice)
class ShopFloorDeviceAdmin(AuditedAdmin):
    list_display = ("name", "is_active", "created_by", "created_at", "last_used_at")
    list_editable = ("is_active",)
    fields = ("name", "is_active", "created_by", "created_at", "last_used_at")
    readonly_fields = ("created_by", "created_at", "last_used_at")

    def has_add_permission(self, request):
        return False  # set up from the PC itself: /shop-floor/setup/


@admin.register(AppSetting)
class AppSettingAdmin(AuditedAdmin):
    list_display = ("key", "value", "updated_by", "updated_at")
    readonly_fields = ("updated_by", "updated_at")

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
