from django.contrib.auth.decorators import login_required
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
