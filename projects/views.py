import csv
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.models import User
from hackathons.models import Hackathon
from projects import permissions, services
from projects.forms import DuplicateProjectForm, ProjectForm
from projects.models import Project
from teams.models import Team


CARD_RELATIONS = (
    'team__memberships__user',
    'evaluations',
    'leaderboard_entries',
    'prize_awards',
    'prizes_won',
)


def _positive_int(value):
    raw = (value or '').strip()
    if raw.isdigit():
        return int(raw)
    return None


def _tech_lookup(name):
    return (
        Q(technologies__iexact=name)
        | Q(technologies__istartswith=f'{name},')
        | Q(technologies__iendswith=f', {name}')
        | Q(technologies__icontains=f', {name},')
    )


def _querystring(request, **updates):
    params = request.GET.copy()
    for key, value in updates.items():
        if value in (None, ''):
            params.pop(key, None)
        else:
            params[key] = str(value)
    encoded = params.urlencode()
    return f'?{encoded}' if encoded else ''


def _visible_or_404(request, pk):
    return get_object_or_404(permissions.visible_projects(request.user), pk=pk)


def _member_initials(project):
    cached = getattr(project.team, '_prefetched_objects_cache', {}).get('memberships')
    if cached is None:
        memberships = project.team.memberships.select_related('user').all()
    else:
        memberships = cached
    initials = [membership.user.initials for membership in list(memberships)[:4]]
    if initials:
        return initials
    captain = project.team.captain
    if captain is not None:
        return [captain.initials]
    return []


def _decorate(project, user, can_create):
    project.can_edit = permissions.can_edit(user, project)
    project.can_delete = permissions.can_delete(user, project)
    project.can_submit = permissions.can_submit(user, project)
    project.can_duplicate = can_create and permissions.can_duplicate(user, project)
    project.member_initials = _member_initials(project)
    return project


def _filter_state(request):
    status = (request.GET.get('status') or '').strip()
    if status not in Project.Status.values:
        status = ''
    view_mode = request.GET.get('view', 'grid')
    if view_mode not in ('grid', 'list'):
        view_mode = 'grid'
    return {
        'q': (request.GET.get('q') or '').strip(),
        'hackathon': _positive_int(request.GET.get('hackathon')) or '',
        'team': _positive_int(request.GET.get('team')) or '',
        'tech': (request.GET.get('tech') or '').strip(),
        'status': status,
        'view': view_mode,
    }


def _narrow(queryset, state, *, include_status):
    if state['q']:
        term = state['q']
        queryset = queryset.filter(
            Q(title__icontains=term)
            | Q(tagline__icontains=term)
            | Q(description__icontains=term)
            | Q(team__name__icontains=term)
            | Q(hackathon__title__icontains=term)
            | Q(technologies__icontains=term)
        )
    if state['hackathon']:
        queryset = queryset.filter(hackathon_id=state['hackathon'])
    if state['team']:
        queryset = queryset.filter(team_id=state['team'])
    if state['tech']:
        queryset = queryset.filter(_tech_lookup(state['tech']))
    if include_status and state['status']:
        queryset = queryset.filter(status=state['status'])
    return queryset


def _stats(queryset):
    return queryset.aggregate(
        total=Count('pk', distinct=True),
        drafts=Count('pk', filter=Q(status=Project.Status.DRAFT), distinct=True),
        in_progress=Count('pk', filter=Q(status=Project.Status.IN_PROGRESS), distinct=True),
        submitted=Count('pk', filter=Q(status=Project.Status.SUBMITTED), distinct=True),
        under_review=Count('pk', filter=Q(status=Project.Status.UNDER_REVIEW), distinct=True),
        evaluated=Count('pk', filter=Q(status=Project.Status.EVALUATED), distinct=True),
        this_week=Count(
            'pk',
            filter=Q(created_at__gte=timezone.now() - timedelta(days=7)),
            distinct=True,
        ),
    )


def _technology_options(queryset):
    found = []
    seen = set()
    for value in queryset.exclude(technologies='').values_list('technologies', flat=True):
        for part in value.split(','):
            name = part.strip()
            key = name.casefold()
            if name and key not in seen:
                seen.add(key)
                found.append(name)
    found.sort(key=str.casefold)
    return found


def _csv_cell(value):
    text = '' if value is None else str(value)
    if text.startswith(('=', '+', '-', '@')):
        return f"'{text}"
    return text


def _apply_form_error(form, exc):
    if getattr(exc, 'message_dict', None):
        for field, errors in exc.message_dict.items():
            target = None if field == '__all__' else field
            for error in errors:
                form.add_error(target, error)
        return
    for error in exc.messages:
        form.add_error(None, error)


def _save_from_form(request, form):
    project = form.save(commit=False)
    try:
        return services.apply_save(
            project,
            actor=request.user,
            intent=request.POST.get('intent', 'save'),
        )
    except services.ProjectActionError as exc:
        form.add_error(None, exc.message)
    except ValidationError as exc:
        _apply_form_error(form, exc)
    return None


@login_required
def project_list(request):
    user = request.user
    base = permissions.visible_projects(user)
    state = _filter_state(request)
    matching = _narrow(base, state, include_status=False)
    projects = list(_narrow(base, state, include_status=True).prefetch_related(*CARD_RELATIONS))
    can_create = permissions.eligible_teams(user).exists()
    for project in projects:
        _decorate(project, user, can_create)

    hackathon_id = state['hackathon']
    team_id = state['team']
    tab_counts = _stats(matching)
    count_for = {
        Project.Status.DRAFT: tab_counts['drafts'],
        Project.Status.IN_PROGRESS: tab_counts['in_progress'],
        Project.Status.SUBMITTED: tab_counts['submitted'],
        Project.Status.UNDER_REVIEW: tab_counts['under_review'],
        Project.Status.EVALUATED: tab_counts['evaluated'],
    }
    tabs = [{
        'value': '',
        'label': _('All'),
        'count': tab_counts['total'],
        'url': _querystring(request, status=None),
        'current': state['status'] == '',
    }]
    for value, label in Project.Status.choices:
        tabs.append({
            'value': value,
            'label': label,
            'count': count_for[value],
            'url': _querystring(request, status=value),
            'current': state['status'] == value,
        })
    chips = []
    if state['q']:
        chips.append({'label': state['q'], 'url': _querystring(request, q=None)})
    if hackathon_id:
        title = Hackathon.objects.filter(pk=hackathon_id).values_list('title', flat=True).first()
        chips.append({'label': title or hackathon_id, 'url': _querystring(request, hackathon=None)})
    if team_id:
        name = Team.objects.filter(pk=team_id).values_list('name', flat=True).first()
        chips.append({'label': name or team_id, 'url': _querystring(request, team=None)})
    if state['tech']:
        chips.append({'label': state['tech'], 'url': _querystring(request, tech=None)})
    if state['status']:
        chips.append({
            'label': Project.Status(state['status']).label,
            'url': _querystring(request, status=None),
        })
    context = {
        'projects': projects,
        'stats': _stats(base),
        'tabs': tabs,
        'chips': chips,
        'filters': state,
        'has_filters': bool(chips),
        'can_create': can_create,
        'hackathons': Hackathon.objects.filter(pk__in=base.values('hackathon_id')).order_by('title'),
        'teams': Team.objects.filter(pk__in=base.values('team_id')).select_related('hackathon').order_by('name'),
        'technologies': _technology_options(base),
        'export_url': reverse('projects:export') + _querystring(request, view=None),
        'grid_url': _querystring(request, view='grid') or '?view=grid',
        'list_url': _querystring(request, view='list') or '?view=list',
        'is_organizer': user.role == User.Role.ORGANIZER,
        'is_jury': user.role == User.Role.JURY,
    }
    return render(request, 'projects/project_list.html', context)


@login_required
def project_export(request):
    state = _filter_state(request)
    projects = _narrow(permissions.visible_projects(request.user), state, include_status=True)
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="projects.csv"'
    response.write('\ufeff')
    writer = csv.writer(response)
    writer.writerow([
        _('Project'), _('Team'), _('Hackathon'), _('Status'), _('Technologies'),
        _('Progress'), _('Repository URL'), _('Demo URL'), _('Updated'),
    ])
    for project in projects:
        writer.writerow([
            _csv_cell(project.title),
            _csv_cell(project.team.name),
            _csv_cell(project.hackathon.title),
            _csv_cell(project.get_status_display()),
            _csv_cell(project.technologies),
            project.progress,
            _csv_cell(project.repo_url),
            _csv_cell(project.demo_url),
            project.updated_at.isoformat() if project.updated_at else '',
        ])
    return response


@login_required
def project_detail(request, pk):
    project = _visible_or_404(request, pk)
    _prefetch_card(project)
    can_create = permissions.eligible_teams(request.user).exists()
    _decorate(project, request.user, can_create)
    average, evaluation_count = services.score_summary(project)
    show_score = average is not None and permissions.can_see_score(request.user, project)
    show_evaluation_count = evaluation_count and (
        permissions.is_event_organizer(request.user, project)
        or request.user.role == User.Role.JURY
        or permissions.is_team_member(request.user, project.team)
    )
    return render(request, 'projects/project_detail.html', {
        'project': project,
        'blockers': services.submission_blockers(project) if project.can_submit else [],
        'can_reopen': permissions.can_reopen(request.user, project),
        'can_review': permissions.can_mark_under_review(request.user, project),
        'can_mark_evaluated': permissions.can_mark_evaluated(request.user, project),
        'score': average if show_score else None,
        'show_score': show_score,
        'evaluation_count': evaluation_count if show_evaluation_count else 0,
        'results_published': project.hackathon.results_published,
        'is_organizer': permissions.is_event_organizer(request.user, project),
    })


def _prefetch_card(project):
    from django.db.models import prefetch_related_objects
    prefetch_related_objects([project], *CARD_RELATIONS)


@login_required
def project_create(request):
    teams = permissions.eligible_teams(request.user)
    if request.method != 'POST' and not teams.exists():
        if request.user.role == User.Role.ORGANIZER:
            messages.info(request, _(
                'No team in your hackathons can open a project right now.'
            ))
        else:
            messages.info(request, _(
                'Join a team in an open hackathon before creating a project.'
            ))
        return redirect('projects:home')

    form = ProjectForm(
        request.user,
        request.POST if request.method == 'POST' else None,
        request.FILES if request.method == 'POST' else None,
    )
    if request.method == 'POST' and form.is_valid():
        project = _save_from_form(request, form)
        if project is not None:
            messages.success(request, _('Project saved.'))
            return redirect('projects:detail', pk=project.pk)
    return render(request, 'projects/project_form.html', {'form': form, 'mode': 'create'})


@login_required
def project_edit(request, pk):
    project = _visible_or_404(request, pk)
    if not permissions.can_edit(request.user, project):
        raise PermissionDenied
    form = ProjectForm(
        request.user,
        request.POST if request.method == 'POST' else None,
        request.FILES if request.method == 'POST' else None,
        instance=project,
    )
    if request.method == 'POST' and form.is_valid():
        saved = _save_from_form(request, form)
        if saved is not None:
            messages.success(request, _('Project saved.'))
            return redirect('projects:detail', pk=saved.pk)
    return render(request, 'projects/project_form.html', {
        'form': form,
        'mode': 'edit',
        'project': project,
    })


@login_required
def project_delete(request, pk):
    project = _visible_or_404(request, pk)
    if not permissions.can_delete(request.user, project):
        raise PermissionDenied
    if request.method == 'POST':
        project.delete()
        messages.success(request, _('Project deleted.'))
        return redirect('projects:home')
    return render(request, 'projects/project_confirm_delete.html', {'project': project})


@login_required
def project_submit(request, pk):
    project = _visible_or_404(request, pk)
    if not permissions.can_submit(request.user, project):
        raise PermissionDenied
    blockers = services.submission_blockers(project)
    if request.method == 'POST' and not blockers:
        try:
            services.submit_project(project, request.user)
        except services.ProjectActionError as exc:
            messages.error(request, exc.message)
            return redirect('projects:submit', pk=project.pk)
        messages.success(request, _('Project submitted. It is now locked for the jury.'))
        return redirect('projects:detail', pk=project.pk)
    return render(request, 'projects/project_submit.html', {
        'project': project,
        'blockers': blockers,
    })


@login_required
def project_duplicate(request, pk):
    project = _visible_or_404(request, pk)
    if not permissions.can_duplicate(request.user, project):
        raise PermissionDenied
    teams = permissions.eligible_teams(request.user)
    form = DuplicateProjectForm(teams, request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and teams.exists() and form.is_valid():
        try:
            copy = services.duplicate_project(project, form.cleaned_data['team'], request.user)
        except services.ProjectActionError as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(request, _('Draft copy created. Files were not copied.'))
            return redirect('projects:edit', pk=copy.pk)
    return render(request, 'projects/project_duplicate.html', {
        'form': form,
        'project': project,
        'has_teams': teams.exists(),
    })


def _mutate(request, pk, allowed, action, success):
    project = _visible_or_404(request, pk)
    if not allowed(request.user, project):
        raise PermissionDenied
    try:
        action(project, request.user)
    except services.ProjectActionError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, success)
    return redirect('projects:detail', pk=project.pk)


@login_required
@require_POST
def project_reopen(request, pk):
    return _mutate(
        request, pk, permissions.can_reopen, services.reopen_project,
        _('Project unlocked. The team can edit it again.'),
    )


@login_required
@require_POST
def project_review(request, pk):
    return _mutate(
        request, pk, permissions.can_mark_under_review, services.mark_under_review,
        _('Project moved to evaluation.'),
    )


@login_required
@require_POST
def project_mark_evaluated(request, pk):
    return _mutate(
        request, pk, permissions.can_mark_evaluated, services.mark_evaluated,
        _('Project marked as evaluated. It can now be ranked with the prizes.'),
    )
