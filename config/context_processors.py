"""Template context shared by every page."""

from config import dashboards


def notification_feed(request):
    """The live "needs your attention" feed used by base.html and the dashboards.

    Computed once per request and only for a signed-in visitor: the bell would
    otherwise show each user a list invented for someone else.
    """
    user = getattr(request, 'user', None)
    if user is None or not getattr(user, 'is_authenticated', False):
        return {}
    groups, summary = dashboards.notification_groups(user)
    return {'notification_feed': groups, 'notification_summary': summary}
