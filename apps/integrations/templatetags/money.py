from django import template

from apps.integrations.square import money as _money

register = template.Library()


@register.filter
def money(cents, currency="CAD"):
    """Cents as dollars: 52500 → $525.00."""
    return _money(cents, currency or "CAD")
