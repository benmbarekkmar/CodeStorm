"""Who can see and change a project.

Rules sit here so the views, the forms and the services stay aligned with
the roles already used by hackathons, teams and prizes:

- participant: works on their own team's project, and can read projects that
  have been submitted in a hackathon they belong to
- organizer: manages every project of the hackathons they organize
- jury: reads submitted projects only; scoring stays in the evaluations app
"""

from django.db.models import Q
from django.utils import timezone

from accounts.models import User
from hackathons.models import Hackathon
from projects.models import Project
from teams.models import Team, TeamMembership


def _cached(instance, relation):
    return getattr(instance, '_prefetched_objects_cache', {}).get(relation)


def related_exists(instance, relation):
    cached = _cached(instance, relation)
    if cached is not None:
        return len(cached) > 0
    return getattr(instance, relation).exists()


def is_event_organizer(user, project):
    return bool(
        user
        and user.is_authenticated
        and project.hackathon.organizer_id == user.pk
    )


def is_team_member(user, team):
    if not user or not user.is_authenticated or team is None:
        return False
    if team.captain_id == user.pk:
        return True
    cached = _cached(team, 'memberships')
    if cached is not None:
        return any(membership.user_id == user.pk for membership in cached)
    return team.has_member(user)


def is_captain(user, team):
    if not user or not user.is_authenticated or team is None:
        return False
    if team.captain_id == user.pk:
        return True
    cached = _cached(team, 'memberships')
    if cached is not None:
        return any(
            membership.user_id == user.pk and membership.role == TeamMembership.Role.CAPTAIN
            for membership in cached
        )
    return team.memberships.filter(user=user, role=TeamMembership.Role.CAPTAIN).exists()


def eligible_teams(user):
    """Teams the user may still open a project for.

    The submission deadline has to be in the future, but the event itself does
    not have to have started: teams prepare drafts before the opening.
    """
    if not user or not user.is_authenticated:
        return Team.objects.none()
    now = timezone.now()
    return (
        Team.objects.filter(
            Q(memberships__user=user) | Q(captain=user) | Q(hackathon__organizer=user),
            hackathon__isnull=False,
            hackathon__status=Hackathon.Status.PUBLISHED,
            hackathon__submission_deadline__gte=now,
            status=Team.Status.ACTIVE,
        )
        .exclude(pk__in=Project.objects.values('team_id'))
        .select_related('hackathon')
        .distinct()
        .order_by('name')
    )


def can_create_for_team(user, team):
    if team is None:
        return False
    return eligible_teams(user).filter(pk=team.pk).exists()


def visible_projects(user):
    """Projects this user is allowed to know about. Drafts stay private."""
    queryset = Project.objects.select_related(
        'hackathon', 'team', 'team__captain', 'submitted_by',
    )
    if not user or not user.is_authenticated:
        return queryset.none()
    if user.is_superuser:
        return queryset
    if user.role == User.Role.ORGANIZER:
        return queryset.filter(hackathon__organizer=user)
    if user.role == User.Role.JURY:
        return queryset.filter(
            hackathon__status=Hackathon.Status.PUBLISHED,
            status__in=Project.RANKED_STATUSES,
        )
    own = Q(team__memberships__user=user) | Q(team__captain=user)
    showcase = Q(
        hackathon__status=Hackathon.Status.PUBLISHED,
        status__in=Project.RANKED_STATUSES,
    ) & (
        Q(hackathon__teams__memberships__user=user) | Q(hackathon__teams__captain=user)
    )
    return queryset.filter(own | showcase).distinct()


def can_view(user, project):
    return visible_projects(user).filter(pk=project.pk).exists()


def _results_are_locked(project):
    if project.hackathon.results_published:
        return True
    return (
        related_exists(project, 'leaderboard_entries')
        or related_exists(project, 'prize_awards')
        or related_exists(project, 'prizes_won')
    )


def can_edit(user, project):
    if not user or not user.is_authenticated or project is None:
        return False
    if project.is_locked or _results_are_locked(project):
        return False
    if is_event_organizer(user, project):
        return True
    if not is_team_member(user, project.team):
        return False
    deadline = project.hackathon.submission_deadline
    if deadline is not None and timezone.now() > deadline:
        return False
    return True


def can_submit(user, project):
    if not user or not user.is_authenticated or project is None or project.is_locked:
        return False
    if not project.hackathon.submission_open:
        return False
    return is_event_organizer(user, project) or is_captain(user, project.team)


def can_delete(user, project):
    if not user or not user.is_authenticated or project is None:
        return False
    if project.is_locked or _results_are_locked(project):
        return False
    if related_exists(project, 'evaluations'):
        return False
    return is_event_organizer(user, project) or is_captain(user, project.team)


def can_duplicate(user, project):
    if not user or not user.is_authenticated or project is None:
        return False
    return is_event_organizer(user, project) or is_team_member(user, project.team)


def can_reopen(user, project):
    """Organizer unlock. Refused once prizes has ranked or awarded the project."""
    if not is_event_organizer(user, project):
        return False
    if project.status not in (Project.Status.SUBMITTED, Project.Status.UNDER_REVIEW):
        return False
    return not _results_are_locked(project)


def can_mark_under_review(user, project):
    if not is_event_organizer(user, project):
        return False
    if project.hackathon.results_published:
        return False
    return project.status == Project.Status.SUBMITTED


def has_complete_score(project):
    """True when the evaluations app has at least one finished score.

    The weighted-score formula stays in ``Evaluation.get_weighted_score``
    so this module does not invent a second rubric.
    """
    evaluations = project.evaluations.filter(is_complete=True).prefetch_related(
        'scores__criterion',
    )
    for evaluation in evaluations:
        if evaluation.get_weighted_score() is not None:
            return True
    return False


def can_mark_evaluated(user, project):
    if not is_event_organizer(user, project):
        return False
    if project.hackathon.results_published:
        return False
    if project.status not in (Project.Status.SUBMITTED, Project.Status.UNDER_REVIEW):
        return False
    return has_complete_score(project)


def can_see_score(user, project):
    """Participants see a score only after prizes publishes the results."""
    if not user or not user.is_authenticated:
        return False
    if is_event_organizer(user, project) or getattr(user, 'role', None) == User.Role.JURY:
        return True
    return bool(project.hackathon.results_published)
