"""Real data behind the three role dashboards and the notification feed.

Every number and every row a dashboard shows comes from a query here. A
dashboard never falls back to a literal: with no data the template renders an
explicit empty state, because a plausible-looking placeholder cannot be told
apart from a real result by the person being graded.

Scopes are the ones the owning modules already define -- a dashboard shows a
user nothing they could not see on that module's own page.
"""

from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from accounts.models import User
from evaluations.models import Evaluation, EvaluationCriterion
from hackathons.models import Hackathon
from prizes.models import LeaderboardEntry
from projects.models import Project
from projects.permissions import visible_projects
from resources.models import Resource
from teams.models import JoinRequest, Team, TeamInvitation

LIST_LIMIT = 5
DEADLINE_LIMIT = 5
DRAFT_EVALUATION_STATUSES = (Evaluation.Status.IN_PROGRESS, Evaluation.Status.COMPLETED)


def _owned_projects(user):
    """Projects of the teams this user belongs to or captains."""
    return (
        Project.objects
        .filter(Q(team__memberships__user=user) | Q(team__captain=user))
        .select_related('hackathon', 'team')
        .distinct()
    )


def _joined_hackathons(user):
    return Hackathon.objects.filter(teams__memberships__user=user).distinct()


def _deadline_rows(hackathons, now):
    """Future deadlines of these hackathons, soonest first."""
    rows = []
    for hackathon in hackathons:
        for label, moment in (
            (_('Team registration closes'), hackathon.registration_deadline),
            (_('Project submission'), hackathon.submission_deadline),
            (_('Event ends'), hackathon.end_date),
        ):
            if moment is None or moment <= now:
                continue
            days = (moment - now).days
            rows.append({
                'label': label,
                'hackathon': hackathon,
                'date': moment,
                'days': days,
                'urgent': days <= 3,
            })
    rows.sort(key=lambda row: row['date'])
    return rows[:DEADLINE_LIMIT]


def _card(label, value, icon, note, tone='bg-primary-fixed text-on-primary-fixed-variant'):
    return {'label': label, 'value': value, 'icon': icon, 'note': note, 'tone': tone}


def _draft_evaluations(user):
    """Evaluations this juré started but did not submit."""
    return (
        Evaluation.objects.filter(jury=user, is_complete=False)
        .exclude(status=Evaluation.Status.PENDING)
        .select_related('project', 'project__hackathon', 'project__team')
        .order_by('-updated_at')
    )


# ---------------------------------------------------------------------------
# Participant
# ---------------------------------------------------------------------------

def participant_context(user):
    now = timezone.now()
    memberships = list(
        user.memberships.select_related(
            'team', 'team__hackathon', 'team__captain',
        ).order_by('-joined_at')
    )
    teams = [membership.team for membership in memberships]
    hackathons = list(_joined_hackathons(user))
    own = _owned_projects(user)
    projects = list(own.order_by('-updated_at')[:LIST_LIMIT])

    active = [h for h in hackathons if h.phase in ('upcoming', 'ongoing')]
    published = [h for h in hackathons if h.results_published]
    ranked_entries = sorted(
        LeaderboardEntry.objects
        .filter(hackathon__in=published, project__in=own)
        .select_related('project', 'project__team', 'hackathon'),
        key=lambda entry: entry.rank,
    )
    scores = [float(entry.final_score) for entry in ranked_entries]
    project_counts = own.aggregate(
        submitted=Count('pk', filter=Q(status__in=Project.RANKED_STATUSES)),
        drafts=Count('pk', filter=Q(status__in=Project.EDITABLE_STATUSES)),
    )
    open_for_me = list(
        Hackathon.objects
        .filter(status=Hackathon.Status.PUBLISHED, registration_deadline__gte=now)
        .exclude(pk__in=[hackathon.pk for hackathon in hackathons])
        .select_related('organizer')
        .order_by('registration_deadline')[:LIST_LIMIT]
    )
    team_pks = {team.pk for team in teams}
    return {
        'stats_cards': [
            _card(_('Active Hackathons'), len(active), 'calendar_month',
                  ngettext('%(n)s event joined', '%(n)s events joined', len(hackathons))
                  % {'n': len(hackathons)}),
            _card(_('My Teams'), len(teams), 'group',
                  _('%(n)s as captain')
                  % {'n': sum(1 for t in teams if t.captain_id == user.pk)},
                  'bg-secondary-fixed/50 text-on-secondary-fixed-variant'),
            _card(_('Submitted Projects'), project_counts['submitted'], 'folder_code',
                  ngettext('%(n)s still a draft', '%(n)s still drafts',
                           project_counts['drafts'])
                  % {'n': project_counts['drafts']},
                  'bg-tertiary-fixed/40 text-on-tertiary-fixed-variant'),
            _card(_('Average Score'),
                  round(sum(scores) / len(scores), 1) if scores else '--',
                  'military_tech',
                  _('published results') if scores else _('not published yet'),
                  'bg-surface-container-highest text-primary'),
        ],
        'teams': teams,
        'team_pks': team_pks,
        'memberships': memberships,
        'hackathons': hackathons,
        'projects': projects,
        'open_hackathons': open_for_me,
        'deadlines': _deadline_rows(hackathons, now),
        'results': ranked_entries[:3],
        'resources': Resource.objects.order_by('-created_at')[:3],
    }


# ---------------------------------------------------------------------------
# Organizer
# ---------------------------------------------------------------------------

def organizer_context(user):
    now = timezone.now()
    managed = list(Hackathon.objects.filter(organizer=user).order_by('-start_date'))
    hackathon_ids = [hackathon.pk for hackathon in managed]
    scope = Q(hackathon_id__in=hackathon_ids)

    phase_counts = {}
    for hackathon in managed:
        phase_counts[hackathon.phase] = phase_counts.get(hackathon.phase, 0) + 1

    project_counts = Project.objects.filter(scope).aggregate(
        drafts=Count('pk', filter=Q(status__in=Project.EDITABLE_STATUSES)),
        ranked=Count('pk', filter=Q(status__in=Project.RANKED_STATUSES)),
    )
    ranked_ids = list(
        Project.objects.filter(scope, status__in=Project.RANKED_STATUSES)
        .values_list('pk', flat=True)
    )
    evaluations = Evaluation.objects.filter(project_id__in=ranked_ids)
    complete_count = evaluations.filter(is_complete=True).count()
    scored_projects = evaluations.filter(is_complete=True).values('project').distinct().count()
    teams_count = Team.objects.filter(scope).count()
    participant_count = (
        User.objects
        .filter(Q(teams__hackathon_id__in=hackathon_ids)
                | Q(teams_captaining__hackathon_id__in=hackathon_ids))
        .distinct().count()
    )
    return {
        'stats_cards': [
            _card(_('Hackathons you manage'), len(managed), 'event',
                  _('%(upcoming)s upcoming, %(ongoing)s running')
                  % {'upcoming': phase_counts.get('upcoming', 0),
                     'ongoing': phase_counts.get('ongoing', 0)}),
            _card(_('Registered participants'), participant_count, 'groups',
                  ngettext('%(n)s team', '%(n)s teams', teams_count)
                  % {'n': teams_count},
                  'bg-secondary-fixed/50 text-on-secondary-fixed-variant'),
            _card(_('Submitted projects'), project_counts['ranked'], 'folder_code',
                  ngettext('%(n)s still a draft', '%(n)s still drafts',
                           project_counts['drafts'])
                  % {'n': project_counts['drafts']},
                  'bg-tertiary-fixed/40 text-on-tertiary-fixed-variant'),
            _card(_('Evaluations to collect'), max(len(ranked_ids) - scored_projects, 0),
                  'how_to_reg',
                  ngettext('%(done)s of %(total)s score submitted',
                           '%(done)s of %(total)s scores submitted', complete_count)
                  % {'done': complete_count, 'total': evaluations.count()},
                  'bg-surface-container-highest text-primary'),
        ],
        'phase_counts': phase_counts,
        'hackathons': managed,
        'ranked_project_count': len(ranked_ids),
        'recent_projects': list(
            Project.objects.filter(scope, status__in=Project.RANKED_STATUSES)
            .select_related('team', 'hackathon')
            .order_by('-submitted_at')[:LIST_LIMIT]
        ),
        'recent_teams': list(
            Team.objects.filter(scope).select_related('hackathon', 'captain')
            .order_by('-created_at')[:LIST_LIMIT]
        ),
        'pending_requests': list(
            JoinRequest.objects.filter(
                team__hackathon_id__in=hackathon_ids,
                status=JoinRequest.Status.PENDING,
            ).select_related('team', 'user')[:LIST_LIMIT]
        ),
        'deadlines': _deadline_rows(managed, now),
        'resources': Resource.objects.order_by('-created_at')[:3],
    }


# ---------------------------------------------------------------------------
# Jury
# ---------------------------------------------------------------------------

def jury_context(user):
    now = timezone.now()
    assignable = visible_projects(user)
    mine = Evaluation.objects.filter(jury=user)
    done_ids = list(mine.filter(is_complete=True).values_list('project_id', flat=True))
    drafts = list(_draft_evaluations(user))
    draft_ids = [evaluation.project_id for evaluation in drafts]
    todo = assignable.exclude(pk__in=done_ids + draft_ids)
    completed = list(
        mine.filter(is_complete=True)
        .select_related('project', 'project__hackathon', 'project__team')
        .prefetch_related('scores__criterion')
        .order_by('-updated_at')
    )
    # The average reads the prefetched cache, so Evaluation.get_weighted_score
    # stays the only scoring formula and the list costs two queries in total.
    # A scored-zero evaluation counts; one without any criterion score does not.
    scores = [
        evaluation.total_score for evaluation in completed
        if evaluation.get_weighted_score() is not None
    ]
    completed = completed[:LIST_LIMIT]
    in_scope = list(
        Hackathon.objects.filter(pk__in={project.hackathon_id for project in assignable})
        .order_by('title')
    )
    criteria_counts = dict(
        EvaluationCriterion.objects
        .filter(hackathon__in=in_scope)
        .values_list('hackathon_id')
        .annotate(count=Count('pk'))
    )
    return {
        'stats_cards': [
            _card(_('Projects you can score'), assignable.count(), 'assignment',
                  ngettext('%(n)s hackathon in scope', '%(n)s hackathons in scope',
                           len(in_scope))
                  % {'n': len(in_scope)}),
            _card(_('Evaluations to start'), todo.count(), 'rate_review',
                  _('not scored by you yet'),
                  'bg-secondary-fixed/50 text-on-secondary-fixed-variant'),
            _card(_('Draft evaluations'), len(drafts), 'edit_note',
                  _('saved but not submitted'),
                  'bg-tertiary-fixed/40 text-on-tertiary-fixed-variant'),
            _card(_('Submitted evaluations'), len(done_ids), 'verified',
                  _('%(avg)s average of your scores')
                  % {'avg': round(sum(scores) / len(scores), 1) if scores else _('none yet')},
                  'bg-surface-container-highest text-primary'),
        ],
        'projects_to_score': list(
            todo.select_related('hackathon', 'team').order_by(
                'hackathon__submission_deadline', '-submitted_at',
            )[:LIST_LIMIT]
        ),
        'drafts_list': drafts[:LIST_LIMIT],
        'recent_evaluations': completed,
        'criteria_rows': [
            {'hackathon': hackathon, 'count': criteria_counts.get(hackathon.pk, 0)}
            for hackathon in in_scope
        ],
        'deadlines': _deadline_rows(in_scope, now),
    }


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notification_groups(user):
    """The feed behind /notifications/, the bell and the "attention" cards.

    There is no Notification model: each entry is a live fact still waiting for
    this user (an invitation to answer, a request to review, results to read).
    An entry disappears as soon as the underlying object is handled, so this
    feed cannot drift away from the data the way a stored log would.

    The channel switches in profile settings are read here, so turning "Team
    Invitations" or "Hackathon Announcements" off has a server-side effect
    instead of only changing a checkbox.
    """
    now = timezone.now()
    invitations = [
        invitation for invitation in TeamInvitation.objects.filter(
            invitee=user, status=TeamInvitation.Status.PENDING,
        ).select_related('team', 'team__hackathon', 'inviter').order_by('-created_at')
        if invitation.expires_at is None or invitation.expires_at > now
    ]
    my_requests = list(
        JoinRequest.objects.filter(
            user=user, status=JoinRequest.Status.PENDING,
        ).select_related('team', 'team__hackathon').order_by('-created_at')
    )
    to_review = list(
        JoinRequest.objects.filter(
            team__captain=user, status=JoinRequest.Status.PENDING,
        ).select_related('team', 'team__hackathon', 'user').order_by('-created_at')
    )
    drafts = list(_draft_evaluations(user))
    ungraded = []
    if getattr(user, 'role', None) == User.Role.ORGANIZER:
        ungraded = list(
            Project.objects.filter(
                hackathon__organizer=user,
                status__in=Project.RANKED_STATUSES,
            )
            .exclude(evaluations__is_complete=True)
            .select_related('hackathon', 'team')
            .order_by('-submitted_at')[:LIST_LIMIT]
        )
    published = list(
        LeaderboardEntry.objects.filter(
            hackathon__results_published=True,
            project__in=_owned_projects(user),
        ).select_related('project', 'hackathon').order_by('hackathon_id', 'rank')
    )
    waiting_submission = list(
        Project.objects.filter(
            Q(team__memberships__user=user) | Q(team__captain=user),
            status__in=Project.RANKED_STATUSES,
            hackathon__results_published=False,
        ).select_related('hackathon', 'team').order_by('-submitted_at')[:10]
    )

    # 'team' and 'hackathon' follow the profile's channel switches; a juré's own
    # draft has no switch -- leaving it unactioned would hide work only they can do.
    groups = [
        {
            'channel': 'team',
            'actionable': True,
            'title': _('Invitations to answer'),
            'icon': 'group_add',
            'items': [
                {
                    'text': _('You were invited to join %(team)s.')
                             % {'team': invitation.team.name},
                    'detail': _('From %(inviter)s on %(event)s.')
                              % {'inviter': invitation.inviter.username,
                                 'event': invitation.team.hackathon.title
                                if invitation.team.hackathon_id else _('no hackathon')},
                    'url': reverse('teams:invitations'),
                    'date': invitation.created_at,
                } for invitation in invitations
            ],
        },
        {
            'channel': 'team',
            'actionable': True,
            'title': _('Requests waiting for your decision'),
            'icon': 'how_to_reg',
            'items': [
                {
                    'text': _('%(user)s asked to join %(team)s.')
                            % {'user': join_request.user.username,
                               'team': join_request.team.name},
                    'detail': join_request.team.hackathon.title
                              if join_request.team.hackathon_id else _('no hackathon'),
                    'url': reverse('teams:detail', args=[join_request.team_id]),
                    'date': join_request.created_at,
                } for join_request in to_review
            ],
        },
        {
            'channel': 'team',
            'actionable': True,
            'title': _('Your pending requests'),
            'icon': 'hourglass_top',
            'items': [
                {
                    'text': _('Your request to join %(team)s is waiting for the captain.')
                            % {'team': join_request.team.name},
                    'detail': join_request.team.hackathon.title
                              if join_request.team.hackathon_id else _('no hackathon'),
                    'url': reverse('teams:detail', args=[join_request.team_id]),
                    'date': join_request.created_at,
                } for join_request in my_requests
            ],
        },
        {
            'channel': 'other',
            'actionable': True,
            'title': _('Evaluations to finish'),
            'icon': 'edit_note',
            'items': [
                {
                    'text': _('Your evaluation of %(project)s is still a draft.')
                            % {'project': evaluation.project.title},
                    'detail': evaluation.project.hackathon.title,
                    'url': reverse('evaluations:detail', args=[evaluation.project_id]),
                    'date': evaluation.updated_at,
                } for evaluation in drafts
            ],
        },
        {
            'channel': 'other',
            'actionable': True,
            'title': _('Submissions still without a submitted score'),
            'icon': 'pending_actions',
            'items': [
                {
                    'text': _('%(project)s has no complete evaluation yet.')
                            % {'project': project.title},
                    'detail': project.hackathon.title,
                    'url': reverse('projects:detail', args=[project.pk]),
                    'date': project.submitted_at or project.updated_at,
                } for project in ungraded
            ],
        },
        {
            'channel': 'hackathon',
            'actionable': False,
            'title': _('Results published for you'),
            'icon': 'workspace_premium',
            'items': [
                {
                    'text': _('%(event)s results are published: %(project)s is rank %(rank)s.')
                            % {'event': entry.hackathon.title, 'project': entry.project.title,
                               'rank': entry.rank},
                    'detail': _('%(score)s points') % {'score': entry.final_score},
                    'url': reverse('prizes:home'),
                    'date': entry.updated_at,
                } for entry in published
            ],
        },
    ]
    enabled = {
        'team': getattr(user, 'notif_team', True),
        'hackathon': getattr(user, 'notif_hackathon', True),
        'other': True,
    }
    visible_groups = [
        {'title': group['title'], 'icon': group['icon'], 'items': group['items']}
        for group in groups
        if group['items'] and enabled[group['channel']]
    ]
    summary = {
        'actionable': sum(
            len(group['items']) for group in groups
            if group['actionable'] and enabled[group['channel']]
        ),
        'waiting_results': len(waiting_submission),
    }
    return visible_groups, summary
