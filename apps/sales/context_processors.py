def new_requests(request):
    """How many requests nobody has looked at yet, for the header."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or not user.has_perm("sales.view_inquiry"):
        return {}
    from .models import Inquiry

    return {"new_requests": Inquiry.objects.filter(status=Inquiry.Status.NEW).count()}
