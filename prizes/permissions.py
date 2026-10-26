"""
prizes/permissions.py
---------------------
Décorateurs et helpers de contrôle d'accès pour le module Prizes.
Utilise le système de rôles de accounts.User :
  - role='organizer'   → organisateur
  - role='jury'        → jury / mentor
  - role='participant' → participant
"""
from functools import wraps
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

from hackathons.models import Hackathon


def role_required(*roles):
    """
    Décorateur de vue qui vérifie que l'utilisateur connecté
    possède l'un des rôles spécifiés.
    Lève PermissionDenied (403) sinon.
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if not request.user.is_authenticated:
                from django.contrib.auth.views import redirect_to_login
                return redirect_to_login(request.get_full_path())
            if request.user.role not in roles:
                raise PermissionDenied
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator


def organizer_required(view_func):
    """Raccourci : seuls les organisateurs peuvent accéder à cette vue."""
    return role_required('organizer')(view_func)


def organizer_or_jury_required(view_func):
    """Raccourci : organisateurs et jury peuvent accéder."""
    return role_required('organizer', 'jury')(view_func)


def get_hackathon_for_organizer(organizer, hackathon_pk):
    """
    Retourne le hackathon s'il appartient à l'organisateur.
    Lève Http404 si non trouvé, PermissionDenied si non propriétaire.
    """
    hackathon = get_object_or_404(Hackathon, pk=hackathon_pk)
    if hackathon.organizer != organizer:
        raise PermissionDenied
    return hackathon


def get_prize_for_organizer(organizer, prize_pk):
    """
    Retourne un prix si son hackathon appartient à l'organisateur.
    Protège contre la manipulation d'ID dans l'URL.
    """
    from prizes.models import Prize
    prize = get_object_or_404(Prize, pk=prize_pk)
    if prize.hackathon.organizer != organizer:
        raise PermissionDenied
    return prize
