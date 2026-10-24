from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

from accounts.models import User


def organizer_required(view_func):
    """Connexion obligatoire + rôle organisateur, sinon 403."""
    @login_required
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if request.user.role != User.Role.ORGANIZER:
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return _wrapped