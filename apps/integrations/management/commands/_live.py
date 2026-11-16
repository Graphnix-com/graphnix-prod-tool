"""--live: read the real Square account for one command, without saving its token.

The token is typed at a hidden prompt and kept in memory for this command only. It never
goes in .env, the shell history or the process list. Used on staging to try real data.
"""

import getpass
import re

from django.conf import settings
from django.core.management.base import CommandError

_LOCATION = re.compile(r"^[A-Z0-9]{8,32}$")


def add_arguments(parser):
    parser.add_argument(
        "--live",
        action="store_true",
        help="Read the real (production) Square account; asks for its access token.",
    )
    parser.add_argument("--location", help="With --live: the production Square location ID.")


def apply(live: bool, location: str | None) -> None:
    if not live:
        if location:
            raise CommandError("--location goes with --live.")
        return
    location = (location or "").strip()
    if not _LOCATION.match(location):
        raise CommandError("--live needs --location <production location ID>.")
    token = getpass.getpass("Square production access token (hidden): ").strip()
    if not token:
        raise CommandError("No token given.")
    settings.SQUARE_ENVIRONMENT = "production"
    settings.SQUARE_ACCESS_TOKEN = token
    settings.SQUARE_LOCATION_ID = location
