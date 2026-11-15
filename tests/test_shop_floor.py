"""The shop-floor PC: name and PIN, only on that PC, locked after 5 wrong tries (NFR-04)."""

import time

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.accounts import shopfloor
from apps.accounts.models import ShopFloorDevice
from apps.audit.models import AuditLog

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def rommel(make_user):
    user = make_user("production", "Rommel")
    user.set_pin("2468")
    user.save()
    return user


@pytest.fixture
def pc(client, admin_user):
    """A browser set up as the shop-floor PC by an Admin."""
    client.force_login(admin_user)
    response = client.post(reverse("shop_floor_setup"), {"name": "Production PC"})
    assert response["Location"] == reverse("shop_floor")
    return client


def pin(client, person, value):
    return client.post(reverse("shop_floor_pin", args=[person.pk]), {"pin": value})


def signed_in_as(client):
    response = client.get(reverse("jobs:list"))
    return response.context["user"] if response.status_code == 200 else None


def test_setup_registers_the_pc_and_signs_the_admin_out(pc, admin_user, rommel):
    device = ShopFloorDevice.objects.get()
    assert device.name == "Production PC" and device.created_by == admin_user
    assert shopfloor.COOKIE in pc.cookies and pc.cookies[shopfloor.COOKIE]["httponly"]
    page = pc.get(reverse("shop_floor"))
    assert not page.context["user"].is_authenticated
    assert "Rommel" in page.content.decode()


def test_only_an_admin_can_set_up_the_pc(client, manager):
    client.force_login(manager)
    assert client.get(reverse("shop_floor_setup")).status_code == 403


def test_name_and_pin_signs_in_and_actions_are_theirs(pc, rommel):
    response = pin(pc, rommel, "2468")
    assert response["Location"] == reverse("home")
    assert signed_in_as(pc) == rommel
    assert AuditLog.objects.filter(entity_id=rommel.pk, action="sign_in").exists()


def test_the_pin_only_works_on_the_shop_floor_pc(client, rommel):
    response = pin(client, rommel, "2468")
    assert response.status_code == 403
    assert "isn't the shop-floor PC" in response.content.decode()
    assert signed_in_as(client) is None
    assert client.get(reverse("shop_floor")).status_code == 403


def test_a_switched_off_pc_stops_offering_names(pc, rommel):
    ShopFloorDevice.objects.update(is_active=False)
    assert pin(pc, rommel, "2468").status_code == 403


def test_five_wrong_tries_lock_the_pin_until_an_admin_resets_it(pc, rommel, admin_user):
    for left in (4, 3, 2, 1):
        assert f"{left} tr" in pin(pc, rommel, "0000").content.decode()
    assert "locked" in pin(pc, rommel, "0000").content.decode()
    # Now even the right PIN is refused.
    assert "locked" in pin(pc, rommel, "2468").content.decode()
    assert signed_in_as(pc) is None
    assert AuditLog.objects.filter(entity_id=rommel.pk, action="pin_locked").count() == 1
    assert "PIN locked" in pc.get(reverse("shop_floor")).content.decode()

    # An Admin sets a new PIN in the admin, on their own PC.
    office = pc.__class__()
    admin_user.is_staff = True
    admin_user.save()
    office.force_login(admin_user)
    url = reverse("admin:accounts_user_change", args=[rommel.pk])
    form = office.get(url).context["adminform"].form
    data = {k: v for k, v in form.initial.items() if v is not None and k != "password"}
    data.update(
        groups=[g.pk for g in rommel.groups.all()],
        user_permissions=[],
        new_pin="1357",
        date_joined_0=rommel.date_joined.strftime("%Y-%m-%d"),
        date_joined_1=rommel.date_joined.strftime("%H:%M:%S"),
    )
    data.pop("date_joined", None)
    data.pop("last_login", None)
    response = office.post(url, data)
    assert response.status_code == 302, response.context["adminform"].form.errors
    rommel.refresh_from_db()
    assert not rommel.pin_locked and rommel.pin_failed_attempts == 0
    assert AuditLog.objects.filter(entity_id=rommel.pk, action="pin_set").exists()
    assert "1357" not in str(list(AuditLog.objects.values_list("changes", flat=True)))

    assert rommel.pin_change_required  # a PIN an Admin set is temporary
    assert pin(pc, rommel, "1357")["Location"] == reverse("shop_floor_change_pin")


def test_a_right_pin_clears_earlier_wrong_tries(pc, rommel):
    pin(pc, rommel, "0000")
    pin(pc, rommel, "2468")
    rommel.refresh_from_db()
    assert rommel.pin_failed_attempts == 0


def test_bad_input_doesnt_count_as_a_try(pc, rommel):
    assert "4-digit" in pin(pc, rommel, "12").content.decode()
    rommel.refresh_from_db()
    assert rommel.pin_failed_attempts == 0


def test_people_without_a_pin_are_not_listed(pc, rommel, make_user):
    make_user("production", "Isaac")  # no PIN yet
    page = pc.get(reverse("shop_floor")).content.decode()
    assert "Rommel" in page and "Isaac" not in page


def test_back_to_the_names_after_ten_idle_minutes(pc, rommel, settings):
    pin(pc, rommel, "2468")
    session = pc.session
    session[shopfloor.SESSION_SEEN] = time.time() - settings.SHOP_FLOOR_IDLE_SECONDS - 1
    session.save()
    response = pc.get(reverse("jobs:list"))
    assert response["Location"] == reverse("shop_floor")
    assert signed_in_as(pc) is None


def test_the_board_refresh_does_not_keep_the_session_alive(pc, rommel, settings):
    pin(pc, rommel, "2468")
    session = pc.session
    stale = time.time() - 60
    session[shopfloor.SESSION_SEEN] = stale
    session.save()
    refresh = pc.get(reverse("jobs:list") + "?partial=1", headers={"X-Background-Refresh": "1"})
    assert refresh.status_code == 200
    assert pc.session[shopfloor.SESSION_SEEN] == stale
    pc.get(reverse("jobs:list"))  # a real click does
    assert pc.session[shopfloor.SESSION_SEEN] > stale


def test_an_expired_session_answers_the_refresh_with_nothing(pc, rommel, settings):
    pin(pc, rommel, "2468")
    session = pc.session
    session[shopfloor.SESSION_SEEN] = time.time() - settings.SHOP_FLOOR_IDLE_SECONDS - 1
    session.save()
    refresh = pc.get(reverse("jobs:list") + "?partial=1", headers={"X-Background-Refresh": "1"})
    assert refresh.status_code == 204


def test_a_shop_floor_session_copied_to_another_browser_is_ended(pc, rommel, client):
    pin(pc, rommel, "2468")
    other = client.__class__()
    other.cookies["sessionid"] = pc.cookies["sessionid"].value  # no shop-floor cookie
    assert other.get(reverse("jobs:list"))["Location"] == reverse("login")


def test_sign_out_and_login_on_the_pc_go_back_to_the_names(pc, rommel):
    pin(pc, rommel, "2468")
    page = pc.get(reverse("jobs:list")).content.decode()
    assert "Done: back to names" in page
    assert pc.post(reverse("logout"))["Location"] == reverse("shop_floor")
    assert pc.get(reverse("login"))["Location"] == reverse("shop_floor")
    assert pc.get(reverse("login") + "?office=1").status_code == 200  # office staff on the PC


def test_tapping_another_name_switches_person(pc, rommel, make_user):
    isaac = make_user("production", "Isaac")
    isaac.set_pin("1111")
    isaac.save()
    pin(pc, rommel, "2468")
    pc.get(reverse("shop_floor"))  # back to the names signs Rommel out
    pin(pc, isaac, "1111")
    assert signed_in_as(pc) == isaac


def test_production_staff_are_added_in_the_admin_with_a_pin_and_no_email(client, admin_user):
    admin_user.is_staff = True
    admin_user.save()
    client.force_login(admin_user)
    response = client.post(
        reverse("admin:accounts_user_add"),
        {"full_name": "Quentin", "email": "", "new_pin": "9753", "usable_password": "false"},
    )
    assert response.status_code == 302, response.context["adminform"].form.errors
    quentin = User.objects.get(full_name="Quentin")
    assert quentin.email is None and not quentin.has_usable_password()
    assert quentin.check_pin("9753") and quentin.pin_change_required


def test_a_password_without_an_email_is_refused_in_the_admin(client, admin_user):
    admin_user.is_staff = True
    admin_user.save()
    client.force_login(admin_user)
    response = client.post(
        reverse("admin:accounts_user_add"),
        {
            "full_name": "Avery",
            "email": "",
            "usable_password": "true",
            "password1": "a-long-password-1",
            "password2": "a-long-password-1",
        },
    )
    assert response.status_code == 200
    assert "needs an email" in response.content.decode()


# --- Choosing their own PIN after an Admin sets one -----------------------------------


@pytest.fixture
def new_starter(make_user):
    """Isaac, with a PIN an Admin just set."""
    user = make_user("production", "Isaac")
    user.set_pin("5050", temporary=True)
    user.save()
    return user


def choose(client, new, again=None):
    return client.post(reverse("shop_floor_change_pin"), {"pin": new, "confirm": again or new})


def test_a_temporary_pin_leads_straight_to_choosing_a_new_one(pc, new_starter):
    assert pin(pc, new_starter, "5050")["Location"] == reverse("shop_floor_change_pin")
    # Nothing else opens until they do.
    assert pc.get(reverse("jobs:list"))["Location"] == reverse("shop_floor_change_pin")
    assert pc.get("/")["Location"] == reverse("shop_floor_change_pin")
    refresh = pc.get(reverse("jobs:list") + "?partial=1", headers={"X-Background-Refresh": "1"})
    assert refresh.status_code == 204


def test_choosing_a_new_pin(pc, new_starter):
    pin(pc, new_starter, "5050")
    assert choose(pc, "8642")["Location"] == reverse("home")
    new_starter.refresh_from_db()
    assert not new_starter.pin_change_required
    assert new_starter.check_pin("8642") and not new_starter.check_pin("5050")
    assert pc.get(reverse("jobs:list")).status_code == 200
    entry = AuditLog.objects.get(entity_id=new_starter.pk, action="pin_changed")
    assert entry.changes is None  # the PIN is never recorded
    # Next time, the new PIN goes straight in.
    pc.get(reverse("shop_floor"))
    assert pin(pc, new_starter, "8642")["Location"] == reverse("home")


@pytest.mark.parametrize(
    ("new", "again", "why"),
    [
        ("8642", "8643", "don&#x27;t match"),
        ("5050", "5050", "different from the one you were given"),
        ("1234", "1234", "too easy"),
        ("7777", "7777", "too easy"),
        ("12a4", "12a4", "4 digits"),
    ],
)
def test_a_poor_new_pin_is_refused(pc, new_starter, new, again, why):
    pin(pc, new_starter, "5050")
    response = choose(pc, new, again)
    assert response.status_code == 400 and why in response.content.decode()
    new_starter.refresh_from_db()
    assert new_starter.pin_change_required and new_starter.check_pin("5050")


def test_walking_away_means_choosing_next_time(pc, new_starter):
    pin(pc, new_starter, "5050")
    pc.get(reverse("shop_floor"))  # back to the names without choosing
    assert pin(pc, new_starter, "5050")["Location"] == reverse("shop_floor_change_pin")


def test_they_can_still_go_back_to_the_names(pc, new_starter):
    pin(pc, new_starter, "5050")
    assert pc.post(reverse("logout"))["Location"] == reverse("shop_floor")


def test_the_new_pin_page_is_only_for_a_temporary_pin_on_the_pc(pc, rommel, client, manager):
    pin(pc, rommel, "2468")  # Rommel chose his own already
    assert pc.get(reverse("shop_floor_change_pin"))["Location"] == reverse("home")
    client.force_login(manager)  # not a shop-floor session
    assert client.get(reverse("shop_floor_change_pin")).status_code == 403


def test_admin_list_shows_a_temporary_pin(new_starter):
    from apps.accounts.admin import UserAdmin

    assert UserAdmin.pin_status(None, new_starter).startswith("Temporary")
