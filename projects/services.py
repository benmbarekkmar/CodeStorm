"""State changes for a project.

Permissions decide who may act. This module checks the same rules again under
a row lock, then writes the status the prizes module already understands.
"""

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from projects.models import Project
from projects import permissions


class ProjectActionError(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


def submission_blockers(project):
    """Reasons a submission must not be locked yet. Empty when it can."""
    blockers = []
    if not (project.title or '').strip():
        blockers.append(_('Add a project name before submitting.'))
    if not (project.description or '').strip():
        blockers.append(_('Add a description before submitting.'))
    if not project.repo_url:
        blockers.append(_('Add a repository URL before submitting.'))
    if not project.hackathon.submission_open:
        blockers.append(_('The submission window for this hackathon is closed.'))
    return blockers


@transaction.atomic
def apply_save(project, *, actor, intent):
    intent = intent if intent in ('draft', 'save') else 'save'
    if project.pk:
        current = (
            Project.objects.select_for_update()
            .select_related('hackathon', 'team')
            .get(pk=project.pk)
        )
        if not permissions.can_edit(actor, current):
            raise ProjectActionError(_('You cannot edit this project.'))
        project.team_id = current.team_id
        project.hackathon_id = current.hackathon_id
        project.submitted_by_id = current.submitted_by_id
        project.submitted_at = current.submitted_at
        project.status = current.status
    else:
        if not permissions.can_create_for_team(actor, project.team):
            raise ProjectActionError(_('You cannot create a project for this team.'))
        project.submitted_by = actor
        project.status = Project.Status.DRAFT
        project.submitted_at = None

    if intent == 'draft':
        project.status = Project.Status.DRAFT
    elif project.status == Project.Status.DRAFT:
        project.status = Project.Status.IN_PROGRESS

    project.full_clean()
    try:
        project.save()
    except IntegrityError as exc:
        raise ProjectActionError(
            _('This team already has a project for this hackathon.')
        ) from exc
    return project


@transaction.atomic
def submit_project(project, actor):
    project = (
        Project.objects.select_for_update()
        .select_related('hackathon', 'team')
        .get(pk=project.pk)
    )
    if not permissions.can_submit(actor, project):
        raise ProjectActionError(_('You cannot submit this project.'))
    blockers = submission_blockers(project)
    if blockers:
        raise ProjectActionError(blockers[0])
    project.status = Project.Status.SUBMITTED
    project.progress = 100
    project.submitted_at = timezone.now()
    project.full_clean()
    project.save()
    return project


@transaction.atomic
def reopen_project(project, actor):
    project = (
        Project.objects.select_for_update()
        .select_related('hackathon', 'team')
        .get(pk=project.pk)
    )
    if not permissions.can_reopen(actor, project):
        raise ProjectActionError(_('This project cannot be reopened.'))
    project.status = Project.Status.IN_PROGRESS
    project.submitted_at = None
    project.save()
    return project


@transaction.atomic
def mark_under_review(project, actor):
    project = (
        Project.objects.select_for_update()
        .select_related('hackathon')
        .get(pk=project.pk)
    )
    if not permissions.can_mark_under_review(actor, project):
        raise ProjectActionError(_('This project cannot be moved to evaluation.'))
    project.status = Project.Status.UNDER_REVIEW
    project.save()
    return project


@transaction.atomic
def mark_evaluated(project, actor):
    project = (
        Project.objects.select_for_update()
        .select_related('hackathon')
        .get(pk=project.pk)
    )
    if not permissions.can_mark_evaluated(actor, project):
        raise ProjectActionError(
            _('A completed jury evaluation is required before this project can be marked evaluated.')
        )
    project.status = Project.Status.EVALUATED
    project.progress = 100
    project.save()
    return project


@transaction.atomic
def duplicate_project(project, team, actor):
    source = Project.objects.select_related('hackathon', 'team').get(pk=project.pk)
    if not permissions.can_duplicate(actor, source):
        raise ProjectActionError(_('You cannot duplicate this project.'))
    if not permissions.can_create_for_team(actor, team):
        raise ProjectActionError(_('Choose a team that does not already have a project.'))
    title = _('Copy of %(title)s') % {'title': source.title}
    copy = Project(
        title=title[:200],
        tagline=source.tagline,
        description=source.description,
        technologies=source.technologies,
        repo_url=source.repo_url,
        demo_url=source.demo_url,
        progress=0,
        hackathon=team.hackathon,
        team=team,
        submitted_by=actor,
        status=Project.Status.DRAFT,
    )
    copy.full_clean()
    try:
        copy.save()
    except IntegrityError as exc:
        raise ProjectActionError(
            _('This team already has a project for this hackathon.')
        ) from exc
    return copy


def score_summary(project):
    """Average of complete jury scores, using the evaluations formula.

    Returns ``(average, count)``. Comments are intentionally not returned.
    """
    scores = []
    evaluations = project.evaluations.filter(is_complete=True).prefetch_related(
        'scores__criterion',
    )
    for evaluation in evaluations:
        value = evaluation.get_weighted_score()
        if value is not None:
            scores.append(value)
    if not scores:
        return None, 0
    return round(sum(scores) / len(scores), 2), len(scores)
