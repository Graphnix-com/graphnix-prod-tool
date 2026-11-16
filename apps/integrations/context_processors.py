def square_inbox(request):
    """How many Square invoices wait to be linked, for the header (FR-04)."""
    user = getattr(request, "user", None)
    if (
        not user
        or not user.is_authenticated
        or not user.has_perm("integrations.link_square_invoice")
    ):
        return {}
    from . import estimates
    from .linking import waiting

    invoices, quotes = waiting().count(), estimates.waiting().count()
    return {
        "invoices_to_link": invoices,
        "estimates_to_link": quotes,
        "square_to_link": invoices + quotes,
    }
