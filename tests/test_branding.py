"""Logo, links between the sign-in pages, and the way back from the admin site."""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_logo_links_home_on_every_page(client):
    page = client.get(reverse("login")).content.decode()
    assert f'<a href="{reverse("home")}" class="brand"><img src="/static/core/logo.png"' in page


def test_login_page_links_to_the_shop_floor_sign_in(client):
    page = client.get(reverse("login")).content.decode()
    assert f'href="{reverse("shop_floor")}"' in page and "Shop-floor sign-in" in page


def test_admin_links_back_to_the_app(client, make_user):
    admin = make_user("admin", "Owner")
    admin.is_staff = True
    admin.save()
    client.force_login(admin)
    page = client.get(reverse("admin:index")).content.decode()
    assert "Back to the app" in page and "GRAPHNIX tracker admin" in page
