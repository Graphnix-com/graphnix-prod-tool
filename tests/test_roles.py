"""Roles, staff accounts and seed data (NFR-01, NFR-02, FR-20)."""

from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command

from apps.accounts import roles
from apps.production.models import DecorationMethod, Station

pytestmark = pytest.mark.django_db
User = get_user_model()


def test_groups_exist_after_migrate():
    names = set(Group.objects.values_list("name", flat=True))
    assert {
        roles.ADMIN,
        roles.MANAGER,
        roles.PRODUCTION,
        roles.QA_AND_SHIPPING,
        roles.CUSTOMER_ENTRY,
    } <= names


def test_every_listed_permission_exists():
    """A typo in roles.py would silently drop a permission; catch it here."""
    for name, perms in roles.ROLE_PERMISSIONS.items():
        group = Group.objects.get(name=name)
        granted = {f"{p.content_type.app_label}.{p.codename}" for p in group.permissions.all()}
        assert set(perms) == granted, name


def test_only_admin_and_manager_see_money(make_user):
    assert make_user("admin").has_perm("jobs.view_money")
    assert make_user("manager").has_perm("jobs.view_money")
    for role in ("production", "avery", "customer_entry"):
        assert not make_user(role).has_perm("jobs.view_money"), role


def test_avery_gets_contacts_and_shipping_but_not_job_editing(make_user):
    avery = make_user("avery")
    assert avery.has_perm("customers.view_contact_details")
    assert avery.has_perm("jobs.move_finish_stages")
    assert avery.has_perm("sales.add_inquiry")
    assert not avery.has_perm("jobs.change_job")
    assert not make_user("production").has_perm("customers.view_contact_details")


def test_only_admin_reopens_cancelled_jobs_and_reads_the_audit_log(make_user):
    assert make_user("admin").has_perm("jobs.reopen_cancelled_job")
    assert make_user("admin").has_perm("audit.view_auditlog")
    assert not make_user("manager").has_perm("jobs.reopen_cancelled_job")
    assert not make_user("manager").has_perm("audit.view_auditlog")


def test_admin_edits_to_a_group_are_reset_by_apply_roles():
    group = Group.objects.get(name=roles.PRODUCTION)
    group.permissions.clear()
    roles.apply_roles()
    assert group.permissions.count() == len(roles.ROLE_PERMISSIONS[roles.PRODUCTION])


def test_seed_staff_is_idempotent():
    call_command("seed_staff", stdout=StringIO())
    call_command("seed_staff", stdout=StringIO())
    assert User.objects.count() == 7
    avery = User.objects.get(full_name="Avery")
    assert set(avery.groups.values_list("name", flat=True)) == {
        roles.PRODUCTION,
        roles.QA_AND_SHIPPING,
        roles.CUSTOMER_ENTRY,
    }
    assert avery.email is None and not avery.has_usable_password()


# --- Production staff accounts ------------------------------------------------------


def test_production_staff_need_no_email_and_several_can_exist():
    User.objects.create_user(email=None, full_name="Rommel")
    User.objects.create_user(email=None, full_name="Isaac")
    assert User.objects.filter(email__isnull=True).count() == 2


def test_a_password_user_still_needs_an_email():
    with pytest.raises(ValueError):
        User.objects.create_user(email=None, password="a-long-password-1", full_name="X")


def test_pin():
    user = User.objects.create_user(email=None, full_name="Quentin")
    assert not user.check_pin("1234")
    user.set_pin("1234")
    assert user.check_pin("1234") and not user.check_pin("4321")
    assert "1234" not in user.pin_hash
    for bad in ("123", "12345", "abcd"):
        with pytest.raises(ValueError):
            user.set_pin(bad)


# --- Methods and stations -------------------------------------------------------


def test_the_ten_methods_are_seeded_each_with_one_station():
    assert DecorationMethod.objects.count() == 10
    for method in DecorationMethod.objects.all():
        assert method.stations.count() == 1, method


def test_a_new_method_gets_its_station():
    method = DecorationMethod.objects.create(code="sublimation", name="Sublimation")
    assert Station.objects.get(decoration_method=method).name == "Sublimation"
    method.save()
    assert method.stations.count() == 1
