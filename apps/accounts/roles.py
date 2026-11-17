"""Roles and extra permissions (NFR-01), as Django groups.

Defined here in code and re-applied after every `migrate`, so staging and production always
match this file. To change what a role can do, edit this file; changes made to these groups in
the admin are overwritten at the next deploy. Who is in which group is set in the admin.

Three roles, plus two extra permissions that are added on top of a role (Avery: Production +
QA and shipping + Customer entry). See the Step 1 spec, "Roles and permissions".
"""

from django.contrib.auth.models import Group, Permission

ADMIN = "Admin/Owner"
MANAGER = "Manager"
PRODUCTION = "Production"
QA_AND_SHIPPING = "QA and shipping"
CUSTOMER_ENTRY = "Customer entry"

# Every permission in these apps goes to Admin/Owner.
ADMIN_APPS = [
    "accounts",
    "audit",
    "core",
    "customers",
    "integrations",
    "jobs",
    "production",
    "sales",
]

_CUSTOMER_EDIT = [
    "customers.add_customer",
    "customers.change_customer",
    "customers.view_customer",
    "customers.add_contact",
    "customers.change_contact",
    "customers.view_contact",
    "customers.add_address",
    "customers.change_address",
    "customers.view_address",
    "customers.view_contact_details",
]
_INQUIRY_EDIT = ["sales.add_inquiry", "sales.change_inquiry", "sales.view_inquiry"]

ROLE_PERMISSIONS: dict[str, list[str]] = {
    MANAGER: [
        "jobs.add_job",
        "jobs.change_job",
        "jobs.view_job",
        "jobs.view_jobstatushistory",
        "jobs.add_jobnote",
        "jobs.view_jobnote",
        "jobs.move_office_stages",
        "jobs.move_production_stages",
        "jobs.move_finish_stages",
        "jobs.override_job_flow",
        "jobs.view_money",
        "jobs.add_jobfile",
        "jobs.view_jobfile",
        "jobs.delete_jobfile",
        "jobs.add_mockupversion",
        "jobs.view_mockupversion",
        "jobs.approve_mockup",
        "integrations.view_squareinvoice",
        "integrations.link_square_invoice",
        "production.assign_staff_task",
        "production.take_task",
        *_CUSTOMER_EDIT,
        *_INQUIRY_EDIT,
        "production.view_decorationmethod",
        "production.view_station",
    ],
    PRODUCTION: [
        "jobs.view_job",
        "jobs.view_mockupversion",
        "jobs.add_jobfile",
        "jobs.view_jobfile",
        "jobs.view_jobstatushistory",
        "jobs.add_jobnote",
        "jobs.view_jobnote",
        "jobs.move_production_stages",
        "production.take_task",
        "production.view_decorationmethod",
        "production.view_station",
    ],
    QA_AND_SHIPPING: [
        "jobs.move_finish_stages",
        "customers.view_customer",
        "customers.view_contact",
        "customers.view_address",
        "customers.view_contact_details",
    ],
    CUSTOMER_ENTRY: [*_CUSTOMER_EDIT, *_INQUIRY_EDIT],
}


def _lookup(names: list[str]) -> list[Permission]:
    found = []
    for name in names:
        app_label, codename = name.split(".")
        perm = Permission.objects.filter(
            content_type__app_label=app_label, codename=codename
        ).first()
        if perm is not None:  # absent only part-way through the first migrate
            found.append(perm)
    return found


def apply_roles() -> None:
    admin, _ = Group.objects.get_or_create(name=ADMIN)
    admin.permissions.set(Permission.objects.filter(content_type__app_label__in=ADMIN_APPS))
    for name, perms in ROLE_PERMISSIONS.items():
        group, _ = Group.objects.get_or_create(name=name)
        group.permissions.set(_lookup(perms))


def apply_roles_after_migrate(sender, **kwargs) -> None:
    """post_migrate handler. Runs once per app; idempotent, and the last run sees every
    permission."""
    apply_roles()
