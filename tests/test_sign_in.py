"""Microsoft 365 sign-in with roles from Entra groups (INT-07)."""

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import entra, roles
from apps.audit.models import AuditLog

pytestmark = pytest.mark.django_db
User = get_user_model()

TENANT = "11111111-2222-3333-4444-555555555555"
ADMIN_GROUP = "aaaaaaaa-0000-0000-0000-000000000001"
MANAGER_GROUP = "aaaaaaaa-0000-0000-0000-000000000002"
PRODUCTION_GROUP = "aaaaaaaa-0000-0000-0000-000000000003"


@pytest.fixture(autouse=True)
def m365(settings):
    settings.M365_TENANT_ID = TENANT
    settings.M365_CLIENT_ID = "client"
    settings.M365_CLIENT_SECRET = "secret"
    settings.M365_GROUP_ADMIN = ADMIN_GROUP
    settings.M365_GROUP_MANAGER = MANAGER_GROUP.upper()  # case doesn't matter
    settings.M365_GROUP_PRODUCTION = PRODUCTION_GROUP
    settings.M365_SIGN_IN_ENABLED = True


def claims(groups=(), email="mckenna@graphnix.example", oid=None, **extra):
    return {
        "tid": TENANT,
        "oid": oid or str(uuid.uuid4()),
        "email": email,
        "name": "McKenna Smith",
        "groups": list(groups),
        **extra,
    }


def role_names(user):
    return set(user.groups.values_list("name", flat=True))


def test_first_sign_in_links_the_seeded_account_by_email():
    seeded = User.objects.create_user(email="mckenna@graphnix.example", full_name="McKenna")
    user = entra.user_for_claims(claims([MANAGER_GROUP]))
    assert user.pk == seeded.pk
    assert user.entra_object_id is not None
    assert role_names(user) == {roles.MANAGER}
    assert user.has_perm("jobs.view_money") and not user.is_staff


def test_next_sign_in_finds_them_by_object_id_even_if_the_email_changed():
    oid = str(uuid.uuid4())
    first = entra.user_for_claims(claims([MANAGER_GROUP], oid=oid))
    again = entra.user_for_claims(
        claims([MANAGER_GROUP], oid=oid, email="m.smith@graphnix.example")
    )
    assert again.pk == first.pk


def test_someone_new_in_a_tracker_group_gets_an_account():
    user = entra.user_for_claims(claims([ADMIN_GROUP], email="Jenn@Graphnix.example", name="Jenn"))
    assert user.email == "jenn@graphnix.example" and user.full_name == "Jenn"
    assert not user.has_usable_password()
    assert role_names(user) == {roles.ADMIN}
    assert user.is_staff  # Admin/Owner manages methods and staff in the admin


def test_roles_follow_the_groups_and_extra_permissions_stay():
    oid = str(uuid.uuid4())
    user = entra.user_for_claims(claims([ADMIN_GROUP], oid=oid))
    user.groups.add(Group.objects.get(name=roles.CUSTOMER_ENTRY))
    user = entra.user_for_claims(claims([MANAGER_GROUP], oid=oid))
    assert role_names(user) == {roles.MANAGER, roles.CUSTOMER_ENTRY}
    assert not user.is_staff


def test_someone_in_no_tracker_group_is_refused_and_loses_their_roles():
    oid = str(uuid.uuid4())
    user = entra.user_for_claims(claims([MANAGER_GROUP], oid=oid))
    with pytest.raises(entra.SignInRefused, match="isn't set up for the tracker"):
        entra.user_for_claims(claims(["some-other-group"], oid=oid))
    assert role_names(User.objects.get(pk=user.pk)) == set()
    assert AuditLog.objects.filter(entity_id=user.pk, action="sign_in_refused").exists()


def test_a_switched_off_account_is_refused():
    oid = str(uuid.uuid4())
    user = entra.user_for_claims(claims([MANAGER_GROUP], oid=oid))
    User.objects.filter(pk=user.pk).update(is_active=False)
    with pytest.raises(entra.SignInRefused, match="switched off"):
        entra.user_for_claims(claims([MANAGER_GROUP], oid=oid))


def test_another_organisation_is_refused():
    with pytest.raises(entra.SignInRefused, match="GRAPHNIX organisation"):
        entra.user_for_claims(claims([ADMIN_GROUP], tid="99999999-0000-0000-0000-000000000000"))
    assert not User.objects.exists()


def test_group_overage_is_refused_with_a_clear_message():
    overage = claims(_claim_names={"groups": "src1"})
    del overage["groups"]
    with pytest.raises(entra.SignInRefused, match="too many groups"):
        entra.user_for_claims(overage)


# --- The pages --------------------------------------------------------------------


def test_login_page_offers_microsoft_and_hides_the_password_form(client):
    page = client.get(reverse("login")).content.decode()
    assert "Sign in with Microsoft 365" in page and 'name="password"' not in page
    assert 'name="password"' in client.get(reverse("login") + "?password=1").content.decode()


def test_login_page_without_microsoft_set_up_shows_the_password_form(client, settings):
    settings.M365_SIGN_IN_ENABLED = False
    page = client.get(reverse("login")).content.decode()
    assert "Microsoft 365" not in page and 'name="password"' in page


def test_start_sends_the_browser_to_microsoft(client, monkeypatch):
    seen = {}

    def fake_start(request, redirect_uri):
        seen["redirect_uri"] = redirect_uri
        return "https://login.microsoftonline.com/authorize?x=1"

    monkeypatch.setattr(entra, "start", fake_start)
    response = client.get(reverse("microsoft_start") + "?next=/jobs/")
    assert response["Location"].startswith("https://login.microsoftonline.com/")
    assert seen["redirect_uri"] == "http://testserver/login/microsoft/callback/"


def test_callback_signs_in_and_returns_to_next(client, monkeypatch):
    monkeypatch.setattr(entra, "start", lambda request, redirect_uri: "https://login.example/")
    monkeypatch.setattr(entra, "finish", lambda request: claims([MANAGER_GROUP]))
    client.get(reverse("microsoft_start") + "?next=/jobs/")
    response = client.get(reverse("microsoft_callback") + "?code=abc&state=xyz")
    assert response["Location"] == "/jobs/"
    assert client.get(reverse("jobs:list")).status_code == 200
    entry = AuditLog.objects.get(action="sign_in")
    assert entry.changes["method"] == [None, "microsoft"]


def test_callback_ignores_a_next_on_another_site(client, monkeypatch):
    monkeypatch.setattr(entra, "start", lambda request, redirect_uri: "https://login.example/")
    monkeypatch.setattr(entra, "finish", lambda request: claims([MANAGER_GROUP]))
    client.get(reverse("microsoft_start") + "?next=https://evil.example/")
    assert client.get(reverse("microsoft_callback"))["Location"] == "/"


def test_callback_refusal_shows_why_and_signs_nobody_in(client, monkeypatch):
    monkeypatch.setattr(entra, "finish", lambda request: claims(["not-a-tracker-group"]))
    response = client.get(reverse("microsoft_callback"), follow=True)
    assert "set up for the tracker" in response.content.decode()
    assert not response.context["user"].is_authenticated


def test_a_callback_without_a_started_sign_in_is_refused(client):
    response = client.get(reverse("microsoft_callback") + "?code=abc&state=xyz", follow=True)
    assert "expired" in response.content.decode()


def test_admin_sign_in_goes_to_the_main_login_page(client):
    response = client.get("/admin/login/?next=/admin/")
    assert response["Location"] == "/login/?next=%2Fadmin%2F"


def test_sign_out_needs_a_post_and_returns_to_login(client, manager):
    client.force_login(manager)
    assert client.get(reverse("logout")).status_code == 405
    assert client.post(reverse("logout"))["Location"] == reverse("login")


class FakeMsal:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def acquire_token_by_auth_code_flow(self, flow, response):
        if self.error:
            raise self.error
        return self.result


def test_finish_turns_a_state_mismatch_into_a_refusal(rf, monkeypatch):
    request = rf.get("/login/microsoft/callback/?code=abc&state=forged")
    request.session = {entra.SESSION_FLOW: {"state": "real"}}
    monkeypatch.setattr(entra, "_client", lambda: FakeMsal(error=ValueError("state mismatch")))
    with pytest.raises(entra.SignInRefused, match="expired"):
        entra.finish(request)


def test_finish_returns_the_id_token_claims(rf, monkeypatch):
    request = rf.get("/login/microsoft/callback/?code=abc&state=s")
    request.session = {entra.SESSION_FLOW: {"state": "s"}}
    good = {"id_token_claims": claims([ADMIN_GROUP])}
    monkeypatch.setattr(entra, "_client", lambda: FakeMsal(result=good))
    assert entra.finish(request)["groups"] == [ADMIN_GROUP]
    assert entra.SESSION_FLOW not in request.session  # a code works once


def test_finish_reports_a_microsoft_error(rf, monkeypatch):
    request = rf.get("/login/microsoft/callback/?error=access_denied")
    request.session = {entra.SESSION_FLOW: {"state": "s"}}
    with pytest.raises(entra.SignInRefused, match="didn't complete"):
        entra.finish(request)
