"""Django settings. Every deploy-specific value comes from the environment (see .env.example)."""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env", overwrite=False)

# --- Core -------------------------------------------------------------------
APP_ENV = env("APP_ENV", default="local")  # local | staging | production
GIT_SHA = env("GIT_SHA", default="dev")
DEBUG = env.bool("DEBUG", default=False)
SECRET_KEY = env("SECRET_KEY")

# localhost is always allowed so the container health check can reach /healthz.
ALLOWED_HOSTS = [*env.list("ALLOWED_HOSTS", default=[]), "localhost", "127.0.0.1"]
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "procrastinate.contrib.django",
    "apps.accounts",
    "apps.audit",
    "apps.core",
    "apps.customers",
    "apps.jobs",
    "apps.m365",
    "apps.integrations",
    "apps.production",
    "apps.sales",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.accounts.middleware.ShopFloorSessionMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.app_info",
                "apps.accounts.context_processors.sign_in",
                "apps.integrations.context_processors.square_inbox",
                "apps.sales.context_processors.new_requests",
            ],
        },
    },
]

# --- Database and cache -----------------------------------------------------
DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# A database cache is shared by every Gunicorn worker, so crash-email throttling
# works across processes. Created by `manage.py createcachetable` on deploy.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "django_cache",
    }
}

# --- Auth -------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "login"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Time and language (NFR-11: store UTC, show Mountain Time) --------------
LANGUAGE_CODE = "en-ca"
TIME_ZONE = "America/Edmonton"
USE_I18N = True
USE_TZ = True

# --- Static and uploaded files ----------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = env.path("MEDIA_ROOT", default=BASE_DIR / "media")
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedManifestStaticFilesStorage"
            if not DEBUG
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        )
    },
}

# --- Security (Caddy terminates HTTPS and redirects HTTP) -------------------
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0)
SESSION_COOKIE_AGE = 60 * 60 * 12  # one shift; shared shop PCs (NFR-04)
# Shop-floor PC: back to the name list after 10 minutes without a click or key (NFR-04).
SHOP_FLOOR_IDLE_SECONDS = env.int("SHOP_FLOOR_IDLE_SECONDS", default=600)
X_FRAME_OPTIONS = "DENY"

# --- Email: crash reports and notifications go out through Microsoft Graph --
M365_TENANT_ID = env("M365_TENANT_ID", default="")
M365_CLIENT_ID = env("M365_CLIENT_ID", default="")
M365_CLIENT_SECRET = env("M365_CLIENT_SECRET", default="")
M365_SENDER = env("M365_SENDER", default="info@graphnix.com")
M365_CONFIGURED = bool(M365_TENANT_ID and M365_CLIENT_ID and M365_CLIENT_SECRET)

# --- Microsoft 365 sign-in (INT-07): same app registration, delegated openid/profile/email.
# The object IDs of the three Entra security groups that give a tracker role. Not secret.
M365_GROUP_ADMIN = env("M365_GROUP_ADMIN", default="")
M365_GROUP_MANAGER = env("M365_GROUP_MANAGER", default="")
M365_GROUP_PRODUCTION = env("M365_GROUP_PRODUCTION", default="")
M365_SIGN_IN_ENABLED = M365_CONFIGURED and bool(M365_GROUP_ADMIN or M365_GROUP_MANAGER)

# Square (Step 2), read-only. Until the token and signature key are set, Square stays off.
# SQUARE_WEBHOOK_URL is the notification URL exactly as entered in Square: the signature
# covers it, so it must match character for character.
SQUARE_ENVIRONMENT = env("SQUARE_ENVIRONMENT", default="sandbox")
SQUARE_ACCESS_TOKEN = env("SQUARE_ACCESS_TOKEN", default="")
SQUARE_WEBHOOK_SIGNATURE_KEY = env("SQUARE_WEBHOOK_SIGNATURE_KEY", default="")
SQUARE_WEBHOOK_URL = env("SQUARE_WEBHOOK_URL", default="")
SQUARE_LOCATION_ID = env("SQUARE_LOCATION_ID", default="")
SQUARE_API_VERSION = env("SQUARE_API_VERSION", default="2025-01-23")
SQUARE_ENABLED = bool(SQUARE_ACCESS_TOKEN and SQUARE_WEBHOOK_SIGNATURE_KEY)
# Square order sources that are online stores: their orders become jobs at Mock-Up
# Approved. Matched ignoring case, anywhere in the source name. Counter sales aren't.
SQUARE_STORE_SOURCES = env.list("SQUARE_STORE_SOURCES", default=["Square Online"])
# Square estimates are read from Square's emails (its API has none): the mailbox they reach
# (the tracker's Microsoft 365 app needs read access to it), and Square's sending addresses.
ESTIMATE_MAILBOX = env("ESTIMATE_MAILBOX", default="")
ESTIMATE_SENDERS = env.list(
    "ESTIMATE_SENDERS",
    default=["invoicing@messaging.squareup.com", "messenger@messaging.squareup.com"],
)
# The website's contact form (GoDaddy) emails info@ too: each becomes a request. Same
# mailbox as the estimates unless set; "off" turns it off.
WEBFORM_MAILBOX = env("WEBFORM_MAILBOX", default=ESTIMATE_MAILBOX)
if WEBFORM_MAILBOX.lower() == "off":
    WEBFORM_MAILBOX = ""
WEBFORM_SENDERS = env.list(
    "WEBFORM_SENDERS", default=["notifications@mail.conversations.godaddy.com"]
)

EMAIL_BACKEND = (
    "apps.m365.email_backend.GraphEmailBackend"
    if M365_CONFIGURED
    else "django.core.mail.backends.console.EmailBackend"
)
DEFAULT_FROM_EMAIL = M365_SENDER
SERVER_EMAIL = M365_SENDER
ADMINS = [("Admin", addr) for addr in env.list("CRASH_EMAIL_TO", default=[])]
EMAIL_SUBJECT_PREFIX = f"[GRAPHNIX tracker {APP_ENV}] "
CRASH_EMAIL_THROTTLE_SECONDS = env.int("CRASH_EMAIL_THROTTLE_SECONDS", default=600)

# --- Health panel -----------------------------------------------------------
BACKUP_STATE_DIR = env.path("BACKUP_STATE_DIR", default=BASE_DIR / "var" / "backup-state")
WORKER_HEARTBEAT_MAX_AGE = 180  # seconds
BACKUP_MAX_AGE_HOURS = 26

# --- Logging ----------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "filters": {"require_debug_false": {"()": "django.utils.log.RequireDebugFalse"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
        "crash_email": {
            "level": "ERROR",
            "filters": ["require_debug_false"],
            "class": "apps.m365.logging.ThrottledAdminEmailHandler",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        # Replaces Django's default "django" logger, whose built-in mail_admins
        # handler would send a second, unthrottled crash email.
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.request": {"handlers": ["crash_email"], "level": "ERROR"},
        # WARNING keeps routine scheduling chatter out of the logs; failed jobs
        # are logged at ERROR and reach the crash email.
        "procrastinate": {"handlers": ["crash_email"], "level": "WARNING"},
        "apps": {"handlers": ["crash_email"], "level": "INFO"},
    },
}
