from django.conf import settings
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

PIN_LENGTH = 4
PIN_MAX_TRIES = 5  # then the PIN is locked until an Admin sets a new one (spec, Roles)
# PINs too easy to guess for someone to choose for themselves.
_EASY_PINS = {d * PIN_LENGTH for d in "0123456789"} | {"1234", "4321", "0123", "3210", "9876"}


class UserManager(BaseUserManager):
    """Office staff sign in with their email address; there is no username.

    Production staff have no Microsoft 365 account and no email (decided 7 Nov 2026): they
    are created without one and sign in with name and PIN on the shop-floor PC (week 4).
    """

    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if email:
            email = self.normalize_email(email).lower()
        elif password is not None:
            raise ValueError("A user who signs in with a password needs an email address.")
        else:
            email = None
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if not email:
            raise ValueError("A superuser must have an email address.")
        if not extra_fields["is_staff"] or not extra_fields["is_superuser"]:
            raise ValueError("A superuser must have is_staff=True and is_superuser=True.")
        return self._create_user(email, password, **extra_fields)

    def get_by_natural_key(self, username):
        return self.get(email__iexact=username)


class User(AbstractUser):
    """Staff account (schema: app_user). Roles are Django groups (NFR-01)."""

    username = None
    first_name = None
    last_name = None

    # NULL (not "") for production staff, so the unique index allows several of them.
    email = models.EmailField("email address", unique=True, null=True, blank=True)  # noqa: DJ001
    full_name = models.CharField(max_length=150)
    # Set the first time someone signs in with Microsoft 365 (INT-07).
    entra_object_id = models.UUIDField(null=True, blank=True, unique=True)
    # The shop-floor PIN, hashed like a password. Works only on a registered shop-floor PC.
    pin_hash = models.CharField(max_length=128, blank=True, editable=False)
    pin_failed_attempts = models.PositiveSmallIntegerField(default=0, editable=False)
    pin_locked_at = models.DateTimeField(null=True, blank=True, editable=False)
    # Set when an Admin sets the PIN; the person picks their own at their next PIN sign-in.
    pin_change_required = models.BooleanField(default=False, editable=False)
    default_station = models.ForeignKey(
        "production.Station", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["full_name"]

    objects = UserManager()

    class Meta:
        db_table = "app_user"
        ordering = ["full_name"]
        constraints = [
            models.UniqueConstraint(Lower("email"), name="app_user_email_ci_unique"),
        ]

    def __str__(self) -> str:
        return self.full_name or self.email

    def clean(self):
        super().clean()
        if self.email:
            self.email = self.__class__.objects.normalize_email(self.email).lower()
        else:
            self.email = None

    def get_full_name(self) -> str:
        return self.full_name

    def get_short_name(self) -> str:
        return self.full_name.split(" ")[0] if self.full_name else self.email

    def set_pin(self, pin: str, *, temporary: bool = False) -> None:
        """`temporary`: set by an Admin, so the person must choose their own next time."""
        if not (len(pin) == PIN_LENGTH and pin.isdigit()):
            raise ValueError(f"A PIN is exactly {PIN_LENGTH} digits.")
        self.pin_hash = make_password(pin)
        self.pin_failed_attempts = 0
        self.pin_locked_at = None
        self.pin_change_required = temporary

    def new_pin_problem(self, pin: str) -> str:
        """Why someone can't choose this PIN for themselves, or "" if they can."""
        if not (len(pin) == PIN_LENGTH and pin.isdigit()):
            return f"A PIN is {PIN_LENGTH} digits."
        if pin in _EASY_PINS:
            return "That PIN is too easy to guess. Pick another."
        if self.check_pin(pin):
            return "Pick a PIN that's different from the one you were given."
        return ""

    def clear_pin(self) -> None:
        self.pin_hash = ""
        self.pin_failed_attempts = 0
        self.pin_locked_at = None
        self.pin_change_required = False

    def check_pin(self, pin: str) -> bool:
        return bool(self.pin_hash) and check_password(pin, self.pin_hash)

    @property
    def has_pin(self) -> bool:
        return bool(self.pin_hash)

    @property
    def pin_locked(self) -> bool:
        return self.pin_locked_at is not None


class AppSetting(models.Model):
    """Admin-editable settings, e.g. working days and the 14-day turnaround (see jobs.workdays)."""

    key = models.CharField(max_length=100, primary_key=True)
    value = models.JSONField()
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "app_setting"
        ordering = ["key"]

    def __str__(self) -> str:
        return self.key


def get_setting(key: str, default=None):
    value = AppSetting.objects.filter(key=key).values_list("value", flat=True).first()
    return default if value is None else value


class ShopFloorDevice(models.Model):
    """A browser registered as the shop-floor PC (NFR-04). It holds a long-lived cookie whose
    hash is stored here; name-and-PIN sign-in works only in a browser with a live one."""

    name = models.CharField(max_length=100, default="Shop-floor PC")
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    is_active = models.BooleanField(
        "active",
        default=True,
        help_text="Untick to stop this browser offering name-and-PIN sign-in.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    last_used_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        db_table = "shop_floor_device"
        ordering = ["-created_at"]
        verbose_name = "shop-floor PC"
        verbose_name_plural = "shop-floor PCs"

    def __str__(self) -> str:
        return self.name
