"""Who may see and launch an AI report.

The rules stay aligned with the roles already used by ``projects`` and
``evaluations``, and reuse ``projects.permissions.visible_projects`` rather than
re-deriving project scoping a second time:

- organizer: sees and launches analyses for the hackathons they organize, plus
  the reports they created themselves
- jury: sees reports attached to projects they are allowed to read, and may
  launch one for such a project, because the report is decision support for the
  score they are about to write
- participant: no access at all -- an AI report is not a participant deliverable
- superuser: reads everything (as in the other modules) but does not launch

An analysis is always launched *from a project*: the repository URL is the one
the team submitted, never a value typed into the browser.
"""

from django.db.models import Q

from accounts.models import User
from analyses.models import AIReport
from projects.models import Project
from projects.permissions import visible_projects

# Only a public github.com URL can ever be fetched by the GitHub service.
GITHUB_URL_PREFIX = 'https://github.com/'


def _role(user):
    return getattr(user, 'role', None)


def has_module_access(user):
    if not user or not user.is_authenticated:
        return False
    return bool(
        user.is_superuser
        or _role(user) in (User.Role.ORGANIZER, User.Role.JURY)
    )


def visible_reports(user):
    """Reports this user is allowed to know about."""
    queryset = AIReport.objects.select_related(
        'created_by', 'project', 'project__hackathon', 'project__team',
    )
    if not user or not user.is_authenticated:
        return queryset.none()
    if user.is_superuser:
        return queryset
    role = _role(user)
    if role == User.Role.ORGANIZER:
        return queryset.filter(
            Q(created_by=user) | Q(project__hackathon__organizer=user)).distinct()
    if role == User.Role.JURY:
        return queryset.filter(
            project_id__in=visible_projects(user).values('pk')).distinct()
    return queryset.none()


def can_view(user, report):
    if report is None:
        return False
    return visible_reports(user).filter(pk=report.pk).exists()


def analyzable_projects(user):
    """Projects whose repository this user may have analyzed.

    Restricted to a fetchable public github.com URL: offering a project whose
    repository lives elsewhere would only produce a guaranteed failure.
    """
    if not user or not user.is_authenticated:
        return Project.objects.none()
    base = Project.objects.filter(
        repo_url__istartswith=GITHUB_URL_PREFIX,
    ).select_related('hackathon', 'team')
    if user.is_superuser:
        return base.none()
    role = _role(user)
    if role == User.Role.ORGANIZER:
        return base.filter(hackathon__organizer=user)
    if role == User.Role.JURY:
        return base.filter(pk__in=visible_projects(user).values('pk'))
    return base.none()


def can_launch(user, project):
    """May this user start (or restart) an analysis of ``project``?

    An analysis is always bound to a project, because the repository it reads is
    the one that project submitted. So the only question is whether this user is
    allowed to have that project read at all: the organizer of its hackathon, or
    a jury member who scores it. A report from before the project link exists
    (``project is None``) can be read but not relaunched -- relaunching it would
    mean analyzing a URL nobody submitted.
    """
    if not user or not user.is_authenticated or user.is_superuser or project is None:
        return False
    return analyzable_projects(user).filter(pk=project.pk).exists()


def can_request_analysis(user):
    """May this user open the analysis form at all?

    This is a role question, not a data question: an organizer or a juré gets the
    entry point even while their hackathon has no project with a repository yet.
    Hiding the button on empty data would make the module look broken; the form
    says what is missing instead.
    """
    if not user or not user.is_authenticated or user.is_superuser:
        return False
    return _role(user) in (User.Role.ORGANIZER, User.Role.JURY)


def can_analyze_anything(user):
    """Is there at least one project this user may have analyzed?

    Drives the empty state of the form: with no fetchable project there is
    nothing to pick, so the submit control stays disabled.
    """
    return analyzable_projects(user).exists()


def can_delete(user, report):
    """The creator may remove their own report; an organizer also removes any
    report of a hackathon they organize. A juré never deletes a report someone
    else produced, because it is shared decision-support evidence.
    """
    if report is None or not user or not user.is_authenticated:
        return False
    if report.created_by_id == user.pk:
        return True
    if _role(user) != User.Role.ORGANIZER:
        return False
    if report.project_id is None:
        return False
    return report.project.hackathon.organizer_id == user.pk
