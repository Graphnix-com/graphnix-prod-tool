import pytest
from django.contrib.auth import authenticate, get_user_model
from django.db import IntegrityError

User = get_user_model()
pytestmark = pytest.mark.django_db


def test_create_user_normalizes_email_and_hashes_password():
    user = User.objects.create_user("Josh@Graphnix.COM", "a-long-password-1", full_name="Josh W")
    assert user.email == "josh@graphnix.com"
    assert user.check_password("a-long-password-1")
    assert not user.is_staff and not user.is_superuser


def test_create_superuser_sets_flags():
    admin = User.objects.create_superuser("owner@graphnix.com", "pw-123456789", full_name="Owner")
    assert admin.is_staff and admin.is_superuser


def test_email_is_unique_regardless_of_case():
    User.objects.create_user("a@graphnix.com", "pw-123456789", full_name="A")
    with pytest.raises(IntegrityError):
        User.objects.create(email="A@GRAPHNIX.com", full_name="Dup")


def test_sign_in_is_case_insensitive():
    User.objects.create_user("staff@graphnix.com", "pw-123456789", full_name="Staff Person")
    assert authenticate(username="STAFF@graphnix.com", password="pw-123456789") is not None


def test_names():
    user = User(email="x@graphnix.com", full_name="Sam Lee")
    assert user.get_full_name() == "Sam Lee"
    assert user.get_short_name() == "Sam"
    assert str(user) == "Sam Lee"
