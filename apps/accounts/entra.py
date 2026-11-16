"""Microsoft 365 sign-in (INT-07) for office staff.

OpenID Connect through MSAL's auth-code flow on the existing Graphnix Tracker app registration
(delegated openid, profile and email; no mailbox access). The tracker role comes from the
person's Entra security groups, read from the ID token's groups claim on every sign-in, so
taking someone out of a group (or out of Microsoft 365) takes their access away.

Only the three role groups are managed here. The extra permissions (QA and shipping, Customer
entry) are set in the tracker's admin and left alone.
"""

from __future__ import annotations

import logging

import msal
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import transaction
from django.utils import timezone

from apps.audit.services import record

from . import roles

log = logging.getLogger(__name__)

SESSION_FLOW = "entra_auth_flow"
# openid and profile are added by MSAL itself; email gives the address to match accounts on.
SCOPES = ["email"]

ROLE_SETTINGS = {
    roles.ADMIN: "M365_GROUP_ADMIN",
    roles.MANAGER: "M365_GROUP_MANAGER",
    roles.PRODUCTION: "M365_GROUP_PRODUCTION",
}


class SignInRefused(Exception):
    """Sign-in failed or was refused. The message is shown to the person."""


def enabled() -> bool:
    return settings.M365_SIGN_IN_ENABLED


def _client() -> msal.ConfidentialClientApplication:
    # Built per sign-in: MSAL fetches the tenant's metadata here, and sign-ins are rare.
    return msal.ConfidentialClientApplication(
        client_id=settings.M365_CLIENT_ID,
        client_credential=settings.M365_CLIENT_SECRET,
        authority=f"https://login.microsoftonline.com/{settings.M365_TENANT_ID}",
    )


def start(request, redirect_uri: str) -> str:
    """Begin a sign-in; returns the Microsoft URL to send the browser to."""
    flow = _client().initiate_auth_code_flow(
        scopes=SCOPES, redirect_uri=redirect_uri, prompt="select_account"
    )
    request.session[SESSION_FLOW] = flow
    return flow["auth_uri"]


def finish(request) -> dict:
    """Swap the code Microsoft sent back for the ID token; returns its claims."""
    flow = request.session.pop(SESSION_FLOW, None)
    if not flow:
        raise SignInRefused("That sign-in link has expired. Please try again.")
    if "error" in request.GET:
        log.warning("Microsoft sign-in error: %s", request.GET.get("error_description", ""))
        raise SignInRefused("Microsoft didn't complete the sign-in. Please try again.")
    try:
        result = _client().acquire_token_by_auth_code_flow(flow, request.GET.dict())
    except (ValueError, RuntimeError) as exc:  # state or nonce mismatch
        log.warning("Microsoft sign-in rejected: %s", exc)
        raise SignInRefused("That sign-in link has expired. Please try again.") from exc
    if "error" in result or "id_token_claims" not in result:
        log.warning(
            "Microsoft sign-in failed: %s: %s", result.get("error"), result.get("error_description")
        )
        raise SignInRefused("Microsoft didn't complete the sign-in. Please try again.")
    return result["id_token_claims"]


# --- From claims to a tracker account ----------------------------------------------


def _role_for_group() -> dict[str, str]:
    mapping = {}
    for role, setting in ROLE_SETTINGS.items():
        group_id = getattr(settings, setting, "").strip().lower()
        if group_id:
            mapping[group_id] = role
    return mapping


def roles_from_claims(claims: dict) -> list[str]:
    if "groups" not in claims and "groups" in (claims.get("_claim_names") or {}):
        # "Group overage": over 200 groups, Entra sends a link instead of the list.
        raise SignInRefused(
            "This Microsoft 365 account is in too many groups for the tracker to read. "
            "An admin needs to check the sign-in setup."
        )
    member_of = {str(g).lower() for g in claims.get("groups") or []}
    mapping = _role_for_group()
    return [role for group_id, role in mapping.items() if group_id in member_of]


def _find_user(object_id: str, email: str):
    User = get_user_model()
    user = User.objects.filter(entra_object_id=object_id).first()
    if user is None and email:
        user = User.objects.filter(email__iexact=email, entra_object_id__isnull=True).first()
    return user


def user_for_claims(claims: dict, ip=None):
    """The tracker account for these ID-token claims, created or brought up to date.
    Raises SignInRefused if this person may not use the tracker."""
    if str(claims.get("tid", "")).lower() != settings.M365_TENANT_ID.strip().lower():
        raise SignInRefused("That Microsoft account isn't in the GRAPHNIX organisation.")
    object_id = claims.get("oid")
    if not object_id:
        raise SignInRefused("Microsoft didn't say who you are. Please try again.")
    email = str(claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    granted = roles_from_claims(claims)
    user = _find_user(object_id, email)

    if not granted:
        if user is not None:
            # Out of every tracker group in Entra: drop the roles here too, so the admin
            # shows it. Saved before refusing.
            with transaction.atomic():
                user.groups.remove(*Group.objects.filter(name__in=ROLE_SETTINGS.keys()))
                if not user.is_superuser and user.is_staff:
                    user.is_staff = False
                    user.save(update_fields=["is_staff"])
                record(None, user, "sign_in_refused", {"reason": [None, "no tracker group"]}, ip=ip)
        log.warning("Microsoft sign-in refused, no tracker group: %s", email or object_id)
        raise SignInRefused(
            "This Microsoft 365 account isn't set up for the tracker. Contact an admin."
        )
    if user is not None and not user.is_active:
        record(None, user, "sign_in_refused", {"reason": [None, "account switched off"]}, ip=ip)
        raise SignInRefused("This tracker account is switched off. Contact an admin.")
    return _update_account(user, object_id, email, claims, granted, ip)


@transaction.atomic
def _update_account(user, object_id, email, claims, granted, ip):
    User = get_user_model()
    if user is None:
        taken = email and User.objects.filter(email__iexact=email).exists()
        user = User(email=None if taken else (email or None))
        user.set_unusable_password()
    user.entra_object_id = object_id
    if not user.email and email and not User.objects.filter(email__iexact=email).exists():
        user.email = email
    if not user.full_name:
        user.full_name = str(claims.get("name") or email or "Microsoft 365 user")[:150]
    # The admin site is for Admin/Owner (methods, staff, audit log). A superuser keeps it.
    user.is_staff = user.is_superuser or roles.ADMIN in granted
    user.last_login = timezone.now()
    user.save()

    role_groups = Group.objects.filter(name__in=ROLE_SETTINGS.keys())
    before = set(user.groups.filter(name__in=ROLE_SETTINGS.keys()).values_list("name", flat=True))
    user.groups.remove(*role_groups.exclude(name__in=granted))
    user.groups.add(*role_groups.filter(name__in=granted))
    changes = {"method": [None, "microsoft"]}
    if before != set(granted):
        changes["roles"] = [sorted(before), sorted(granted)]
    record(user, user, "sign_in", changes, ip=ip)
    return user
