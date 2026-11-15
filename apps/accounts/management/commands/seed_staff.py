"""Create the shop's staff accounts with their roles. Safe to run again.

    python manage.py seed_staff

Accounts are created without email or password. Before Microsoft 365 sign-in goes live
(Step 1, week 4), add Jenn's, Nick's and McKenna's work emails in the admin. Production staff
keep no email: they sign in with name and PIN on the shop-floor PC.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts import roles

# From the Step 1 spec, "Roles and permissions" (answered 7 Nov 2026).
STAFF = [
    ("Jenn", [roles.ADMIN]),
    ("Nick", [roles.ADMIN]),
    ("McKenna", [roles.MANAGER]),
    ("Avery", [roles.PRODUCTION, roles.QA_AND_SHIPPING, roles.CUSTOMER_ENTRY]),
    ("Rommel", [roles.PRODUCTION]),
    ("Isaac", [roles.PRODUCTION]),
    ("Quentin", [roles.PRODUCTION]),
]


class Command(BaseCommand):
    help = "Create the shop's staff accounts and put each in their groups."

    @transaction.atomic
    def handle(self, *args, **options):
        roles.apply_roles()
        User = get_user_model()
        for full_name, group_names in STAFF:
            user = User.objects.filter(full_name__iexact=full_name).first()
            created = user is None
            if created:
                user = User.objects.create_user(email=None, full_name=full_name)
            user.groups.add(*Group.objects.filter(name__in=group_names))
            verb = "created" if created else "already there"
            self.stdout.write(f"{full_name}: {verb}, groups {', '.join(group_names)}")
