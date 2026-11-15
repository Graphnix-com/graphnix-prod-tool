"""Production never sees money (NFR-02).

Payment status and amounts are only shown to people with jobs.view_money. The other way money
could reach Production is typed text they can read: job titles, items, task notes, job notes
and the reasons given for a move. Those refuse anything that looks like a dollar amount.
"""

import re

from django.core.exceptions import ValidationError

MESSAGE = "Leave prices out: Production can read this. Prices stay in Square."

_AMOUNT = re.compile(
    r"\$\s*\d|\d\s*\$|\b\d[\d,.]*\s*(?:dollars?|bucks|cad)\b|\bcad\s*\d", re.IGNORECASE
)


def has_amount(text: str | None) -> bool:
    return bool(text) and bool(_AMOUNT.search(text))


def no_amounts(text: str | None) -> None:
    """Validator for fields Production can read."""
    if has_amount(text):
        raise ValidationError(MESSAGE, code="money")
