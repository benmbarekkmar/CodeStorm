from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import render

from accounts.models import User
from config import dashboards

ROLE_DASHBOARDS = {
    User.Role.ORGANIZER: ('organizer_dashboard.html', dashboards.organizer_context),
    User.Role.JURY: ('jury_dashboard.html', dashboards.jury_context),
    User.Role.PARTICIPANT: ('participant_dashboard.html', dashboards.participant_context),
}


@login_required
def dashboard(request):
    """The landing page of each role, built entirely from live queries."""
    template, build = ROLE_DASHBOARDS.get(
        getattr(request.user, 'role', None), ('dashboard.html', dashboards.participant_context),
    )
    return render(request, template, build(request.user))


@login_required
def notifications(request):
    """The feed itself comes from the notification_feed context processor."""
    return render(request, 'notifications.html')


def healthz(request):
    """Liveness probe: proves the process answers, nothing more.

    A database round trip here would restart a healthy app every time the
    database blinks, so this stays a plain 200.
    """
    return JsonResponse({'status': 'ok'})


@login_required
def media_file(request, uploaded_path):
    """Serve one uploaded file, to a signed-in user only.

    ``/media/`` used to be a public directory. The URL stays the same -- it is
    the one stored in ``banner``, ``avatar``, ``pitch_deck`` -- but the files are
    now reachable exactly where the pages showing them are.
    """
    root = Path(settings.MEDIA_ROOT).resolve()
    target = (root / uploaded_path).resolve()
    if root == target or root not in target.parents:
        raise Http404
    if not target.is_file():
        raise Http404
    return FileResponse(target.open('rb'))
