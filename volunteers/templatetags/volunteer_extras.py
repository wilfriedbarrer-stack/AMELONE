from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def euros(cents):
    try:
        amount = Decimal(int(cents)) / 100
    except (TypeError, ValueError):
        amount = Decimal(0)
    return f"{amount:.2f}".replace(".", ",") + " €"


@register.filter
def lookup(mapping, key):
    if mapping is None:
        return None
    return mapping.get(key)

