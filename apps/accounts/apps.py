from django.apps import AppConfig
from django.db.models.signals import post_migrate


class AccountsConfig(AppConfig):
    name = "apps.accounts"
    label = "accounts"
    verbose_name = "Accounts"

    def ready(self):
        from .roles import apply_roles_after_migrate

        post_migrate.connect(apply_roles_after_migrate, dispatch_uid="accounts.apply_roles")
