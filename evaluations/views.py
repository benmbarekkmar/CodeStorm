"""Évaluations du jury : tableau de bord, notation pondérée, résultats et export.

Adapté du travail d'Oussama (branche ``oussama``) à l'architecture partagée de
``develop`` : les projets viennent de ``projects``, les hackathons de
``hackathons``, et la note agrégée reste la formule de
:meth:`evaluations.models.Evaluation.get_weighted_score` déjà utilisée par
``prizes/services.py``. Aucune table n'est dupliquée ici.

Règles d'accès appliquées côté serveur (le template ne fait que masquer) :

* seul le jury note, et uniquement ses propres évaluations ;
* l'organisateur voit l'ensemble du travail du jury, y compris les notes
  confidentielles, mais ne note pas à sa place ;
* un participant n'accède à aucune donnée d'évaluation ;
* les classements et l'export ne révèlent un hackathon qu'après
  ``Hackathon.results_published``, sauf pour l'organisateur qui le publie.
"""
from __future__ import annotations

import csv
import json
import statistics

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Prefetch
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_GET, require_POST

from accounts.models import User
from hackathons.models import Hackathon
from projects.models import Project

from .models import (
    Evaluation, EvaluationComment, EvaluationCriterion, EvaluationScore,
)

# Rubrique de secours proposée au jury quand l'organisateur n'a encore défini
# aucun critère. Elle est rattachée au hackathon concerné (jamais globale) :
# prizes/services.py n'accepte que les critères du même hackathon.
DEFAULT_RUBRIC = [
    (_("Innovation & Creativity"),
     _("Novelty, uniqueness of concept, and creative problem-solving approach."), 20, 1),
    (_("Technical Implementation"),
     _("Architecture soundness, code quality, stability, API integration depth."), 20, 2),
    (_("User Experience & Design Polish"),
     _("Interface polish, intuitive task flow, accessibility, and visual feedback."), 20, 3),
    (_("Impact & Relevance"),
     _("Problem clarity, market viability, direct target audience utility."), 20, 4),
    (_("Presentation & Documentation"),
     _("Clarity of demo video, pitch deck coherence, and README completeness."), 20, 5),
]

# Les projets en brouillon appartiennent à l'équipe : ils ne sont jamais
# proposés au jury.
_EVALUABLE_STATUSES = (
    Project.Status.SUBMITTED,
    Project.Status.UNDER_REVIEW,
    Project.Status.EVALUATED,
)


def _is_jury(user):
    return user.role == User.Role.JURY


def _is_organizer(user):
    """Organisateur du rôle, ou administrateur de la plateforme.

    Un superutilisateur a déjà accès à toutes les fiches en /admin/ : le lui
    reconnaître ici n'ouvre rien de nouveau, alors qu'un participant au rôle
    « participant » reste hors de cet espace, y compris s'il est staff.
    """
    return user.role == User.Role.ORGANIZER or user.is_superuser


def scoring_required(view):
    """Seul le jury peut ouvrir/modifier une fiche d'évaluation."""
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            raise PermissionDenied
        if not _is_jury(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    wrapper.__name__ = view.__name__
    wrapper.__doc__ = view.__doc__
    return wrapper


def evaluation_body_required(view):
    """Jury et organisateurs : les deux sont habilités à lire le travail d'évaluation."""
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            raise PermissionDenied
        if not (_is_jury(request.user) or _is_organizer(request.user)):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    wrapper.__name__ = view.__name__
    wrapper.__doc__ = view.__doc__
    return wrapper


def rubric_for(hackathon):
    """Les critères de ce hackathon, dans l'ordre d'affichage."""
    return EvaluationCriterion.objects.filter(
        hackathon=hackathon,
    ).order_by('order', 'id')


def ensure_rubric(hackathon):
    """Return the hackathon rubric, creating the documented default one if empty.

    Writing happens only from an explicit jury action, never from a page render,
    so simply opening the dashboard cannot add rows to an organizer's rubric.
    """
    criteria = list(rubric_for(hackathon))
    if criteria:
        return criteria
    criteria = EvaluationCriterion.objects.bulk_create([
        EvaluationCriterion(
            hackathon=hackathon, name=str(name), description=str(description),
            weight=weight, order=order,
        )
        for name, description, weight, order in DEFAULT_RUBRIC
    ])
    return sorted(criteria, key=lambda c: (c.order, c.pk))


def evaluation_queryset():
    return Evaluation.objects.select_related(
        'project', 'project__hackathon', 'project__team', 'jury',
    ).prefetch_related(
        Prefetch('scores', queryset=EvaluationScore.objects.select_related('criterion'))
    )


def visible_evaluations(user):
    """Ce que cet utilisateur est autorisé à voir, avec la même requête pour tous."""
    qs = evaluation_queryset()
    return qs if _is_organizer(user) else qs.filter(jury=user)


def build_catalog(user):
    """Hackathons + projets évaluables, avec l'état d'avancement du jury."""
    own_evaluated = set(
        Evaluation.objects.filter(jury=user).values_list('project_id', flat=True)
    )
    own_completed = set(
        Evaluation.objects.filter(jury=user, is_complete=True)
        .values_list('project_id', flat=True)
    )
    catalog = []
    hackathons = Hackathon.objects.exclude(
        status=Hackathon.Status.CANCELLED,
    ).prefetch_related(
        Prefetch('projects', queryset=Project.objects.filter(
            status__in=_EVALUABLE_STATUSES,
        ).select_related('team').order_by('title'))
    ).order_by('-start_date')
    for hackathon in hackathons:
        projects = [{
            'id': project.pk,
            'name': project.title,
            'team_name': project.team.name,
            'started': project.pk in own_evaluated,
            'completed': project.pk in own_completed,
        } for project in hackathon.projects.all()]
        if not projects:
            continue
        catalog.append({
            'id': hackathon.pk,
            'name': hackathon.title,
            'track': hackathon.get_category_display() or '',
            'projects': projects,
        })
    return catalog


def _pct(part, whole):
    if not whole:
        return 0
    return round((part / whole) * 100)


# ---------------------------------------------------------------------------
# Main evaluations page
# ---------------------------------------------------------------------------

@login_required
@evaluation_body_required
def evaluations_home(request):
    user = request.user
    evals = list(visible_evaluations(user).order_by('-updated_at'))
    pending_list = [e for e in evals if e.status in (
        Evaluation.Status.PENDING, Evaluation.Status.IN_PROGRESS)]
    completed_list = [e for e in evals if e.status == Evaluation.Status.COMPLETED]
    my_evals = [e for e in evals if e.jury_id == user.pk]

    total_projects = Project.objects.filter(status__in=_EVALUABLE_STATUSES).count()
    pending_evals = sum(1 for e in evals if e.status == Evaluation.Status.PENDING)
    inprogress_evals = sum(1 for e in evals if e.status == Evaluation.Status.IN_PROGRESS)
    completed_evals = len(completed_list)
    total_evals = len(evals)
    queue_count = pending_evals + inprogress_evals

    completed_scores = [e.total_score for e in completed_list]
    avg_score = round(sum(completed_scores) / len(completed_scores)) if completed_scores else 0
    median_score = round(statistics.median(completed_scores), 1) if completed_scores else 0
    stdev_score = round(statistics.pstdev(completed_scores), 1) if len(completed_scores) > 1 else 0

    dist = {'exceptional': 0, 'proficient': 0, 'needs_work': 0, 'unsatisfactory': 0}
    for score in completed_scores:
        if score >= 85:
            dist['exceptional'] += 1
        elif score >= 70:
            dist['proficient'] += 1
        elif score >= 50:
            dist['needs_work'] += 1
        else:
            dist['unsatisfactory'] += 1

    scored_pct = _pct(completed_evals, total_evals)
    dist_total = completed_evals or 1
    dist_pct = {key: _pct(value, completed_evals) for key, value in dist.items()}

    now = timezone.now()
    next_hackathon = (
        Hackathon.objects.exclude(status=Hackathon.Status.CANCELLED)
        .filter(submission_deadline__gte=now)
        .order_by('submission_deadline')
        .first()
    )

    hackathons = list(
        Hackathon.objects.exclude(status=Hackathon.Status.CANCELLED)
        .order_by('-start_date')
    )
    # Les hackathons hérités peuvent avoir une category NULL: get_FOO_display()
    # rend alors None, ce qui casserait le tri des puces de filtre.
    tracks = sorted({display for display in (
        h.get_category_display() for h in hackathons
    ) if display})

    # Une seule requête pour le nombre de critères par hackathon : le pourcentage
    # d'avancement d'un brouillon se lit sans toucher la base ligne par ligne.
    criteria_totals = {
        row['hackathon_id']: row['total']
        for row in EvaluationCriterion.objects.values('hackathon_id').annotate(total=Count('id'))
    }
    for ev in evals:
        scored = list(ev.scores.all())
        ev.scores_json = json.dumps([
            {'name': s.criterion.name, 'score': int(s.score)}
            for s in scored
        ])
    for ev in my_evals:
        ev.scored_criteria = len(ev.scores.all())
        ev.criteria_total = criteria_totals.get(ev.project.hackathon_id, 0)
        ev.draft_pct = _pct(ev.scored_criteria, ev.criteria_total)

    context = {
        'total_projects': total_projects,
        'pending_evals': pending_evals,
        'inprogress_evals': inprogress_evals,
        'completed_evals': completed_evals,
        'queue_count': queue_count,
        'total_evals': total_evals,
        'avg_score': avg_score,
        'median_score': median_score,
        'stdev_score': stdev_score,
        'pending_list': pending_list,
        'completed_list': completed_list,
        'my_evals': my_evals,
        'recent_feed': completed_list[:8],
        'dist': dist,
        'dist_pct': dist_pct,
        'dist_total': dist_total,
        'scored_pct': scored_pct,
        'completed_pct': scored_pct,
        'inprogress_pct': _pct(inprogress_evals, total_evals),
        'pending_pct': _pct(pending_evals, total_evals),
        'hackathons': hackathons,
        'tracks': tracks,
        'catalog': build_catalog(user),
        'next_hackathon': next_hackathon,
        'now': now,
        'is_organizer': _is_organizer(user),
        'page': 'evaluations',
    }
    return render(request, 'evaluations.html', context)


# ---------------------------------------------------------------------------
# Catalog (hackathons + projects for the start picker)
# ---------------------------------------------------------------------------

@login_required
@evaluation_body_required
@require_GET
def evaluation_catalog(request):
    return JsonResponse({'hackathons': build_catalog(request.user)})


# ---------------------------------------------------------------------------
# Start evaluation for a chosen project
# ---------------------------------------------------------------------------

@login_required
@scoring_required
@require_POST
def evaluation_start(request):
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    project_id = body.get('project_id')
    if not project_id:
        return JsonResponse({'error': str(_('Please choose a project to evaluate.'))}, status=400)

    project = get_object_or_404(
        Project.objects.select_related('hackathon', 'team'),
        pk=project_id,
        status__in=_EVALUABLE_STATUSES,
    )
    ensure_rubric(project.hackathon)
    evaluation, created = Evaluation.objects.get_or_create(
        project=project,
        jury=request.user,
        defaults={'status': Evaluation.Status.IN_PROGRESS},
    )
    if evaluation.status == Evaluation.Status.PENDING:
        evaluation.status = Evaluation.Status.IN_PROGRESS
        evaluation.save(update_fields=['status', 'updated_at'])

    return JsonResponse({
        'success': True,
        'created': created,
        'evaluation_id': evaluation.pk,
        'project_id': project.pk,
        'status': evaluation.status,
    })


# ---------------------------------------------------------------------------
# Load one evaluation (read-only JSON for the scoring modal)
# ---------------------------------------------------------------------------

@login_required
@scoring_required
@require_GET
def evaluation_detail(request, project_id):
    """Return the jury's own evaluation + rubric as JSON (AJAX modal).

    Lecture seule : la fiche est créée par ``evaluation:start``, donc un simple
    affichage de page n'écrit jamais en base.
    """
    project = get_object_or_404(
        Project.objects.select_related('hackathon', 'team'),
        pk=project_id,
        status__in=_EVALUABLE_STATUSES,
    )
    evaluation = get_object_or_404(
        evaluation_queryset(), project=project, jury=request.user,
    )

    criteria_data = []
    score_by_criterion = {s.criterion_id: s.score for s in evaluation.scores.all()}
    for criterion in rubric_for(project.hackathon):
        criteria_data.append({
            'id': criterion.pk,
            'name': criterion.name,
            'description': criterion.description,
            'weight': criterion.weight,
            'score': int(score_by_criterion.get(criterion.pk, 5)),
        })

    data = {
        'project_id': project.pk,
        'project_name': project.title,
        'team_name': project.team.name,
        'hackathon': project.hackathon.title,
        'github_url': project.repo_url,
        'demo_url': project.demo_url,
        'evaluation_id': evaluation.pk,
        'status': evaluation.status,
        'strengths': evaluation.strengths,
        'improvements': evaluation.improvements,
        'confidential_notes': evaluation.confidential_notes,
        'criteria': criteria_data,
        'total_score': evaluation.total_score,
    }
    return JsonResponse(data)


# ---------------------------------------------------------------------------
# Submit / save-draft evaluation
# ---------------------------------------------------------------------------

@login_required
@scoring_required
@require_POST
def evaluation_submit(request):
    """Save or submit an evaluation (JSON body)."""
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    evaluation_id = body.get('evaluation_id')
    action = body.get('action', 'draft')  # 'draft' | 'submit'

    # Le filtre sur jury= est la seule autorité : un juré ne peut pas écrire sur
    # la fiche d'un autre, même avec un id deviné.
    evaluation = get_object_or_404(Evaluation, pk=evaluation_id, jury=request.user)

    evaluation.strengths = body.get('strengths', '')
    evaluation.improvements = body.get('improvements', '')
    evaluation.confidential_notes = body.get('confidential_notes', '')

    allowed_ids = {
        criterion.pk for criterion in ensure_rubric(evaluation.project.hackathon)
    }
    scores_data = body.get('scores', {})
    for criteria_id_str, score_val in scores_data.items():
        try:
            criteria_id = int(criteria_id_str)
            if criteria_id not in allowed_ids:
                continue
            score_val = max(0, min(10, int(score_val)))
        except (ValueError, TypeError):
            continue
        EvaluationScore.objects.update_or_create(
            evaluation=evaluation,
            criterion_id=criteria_id,
            defaults={'score': score_val},
        )

    if action == 'submit':
        evaluation.status = Evaluation.Status.COMPLETED
        evaluation.is_complete = True
    else:
        evaluation.status = Evaluation.Status.IN_PROGRESS

    evaluation.save()

    return JsonResponse({
        'success': True,
        'status': evaluation.status,
        'total_score': evaluation.total_score,
    })


# ---------------------------------------------------------------------------
# Add comment
# ---------------------------------------------------------------------------

@login_required
@evaluation_body_required
@require_POST
def evaluation_add_comment(request, evaluation_id):
    """Add a comment to an evaluation the requester may already see."""
    evaluation = get_object_or_404(visible_evaluations(request.user), pk=evaluation_id)
    text = request.POST.get('text', '').strip()
    if not text:
        return JsonResponse({'error': str(_('The comment cannot be empty.'))}, status=400)

    comment = EvaluationComment.objects.create(
        evaluation=evaluation,
        author=request.user,
        text=text,
    )
    return JsonResponse({
        'success': True,
        'comment': {
            'id': comment.pk,
            'author': comment.author.get_full_name() or comment.author.username,
            'text': comment.text,
            'created_at': comment.created_at.strftime('%b %d, %Y • %H:%M'),
        }
    })


# ---------------------------------------------------------------------------
# Leaderboard / Results
# ---------------------------------------------------------------------------

@login_required
@evaluation_body_required
def results_leaderboard(request):
    """Classement par note pondérée, dans le même sens que prizes/services.py.

    Pour un juré, seuls les hackathons dont les résultats sont publiés
    apparaissent : avant la publication, un score de projet n'est pas une
    information à divulguer. L'organisateur qui publie voit tout.
    """
    hackathon_id = request.GET.get('hackathon', '')

    visible_hackathons = Hackathon.objects.exclude(status=Hackathon.Status.CANCELLED)
    if not _is_organizer(request.user):
        visible_hackathons = visible_hackathons.filter(results_published=True)

    # Restreindre d'abord l'ensemble, puis le choix de l'utilisateur : un
    # ``?hackathon=<id>`` deviné ne doit jamais sortir du périmètre autorisé.
    visible_ids = set(visible_hackathons.values_list('pk', flat=True))
    evals = visible_evaluations(request.user).filter(
        is_complete=True, project__hackathon_id__in=visible_ids,
    )
    if hackathon_id:
        visible_hackathons = visible_hackathons.filter(pk=hackathon_id)
        evals = evals.filter(project__hackathon_id=hackathon_id)

    ranked = sorted(
        evals,
        key=lambda e: (-e.total_score, e.project_id, e.pk),
    )

    context = {
        'ranked': ranked,
        'hackathons': visible_hackathons.order_by('-start_date'),
        'selected_hackathon': hackathon_id,
        'is_organizer': _is_organizer(request.user),
        'page': 'evaluations',
    }
    return render(request, 'evaluations_results.html', context)


# ---------------------------------------------------------------------------
# Export CSV
# ---------------------------------------------------------------------------

@login_required
@evaluation_body_required
def export_csv(request):
    """Export the completed evaluations this user is allowed to read."""
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="evaluations_export.csv"'

    is_organizer = _is_organizer(request.user)
    writer = csv.writer(response)
    header = [
        'Project', 'Team', 'Hackathon', 'Evaluator',
        'Total Score', 'Status', 'Strengths', 'Improvements', 'Date',
    ]
    if is_organizer:
        header.insert(8, 'Notes')
    writer.writerow(header)

    evals = visible_evaluations(request.user).filter(
        is_complete=True,
    ).order_by('-updated_at')

    for ev in evals:
        row = [
            ev.project.title,
            ev.project.team.name,
            ev.project.hackathon.title,
            ev.jury.get_full_name() or ev.jury.username,
            ev.total_score,
            ev.get_status_display(),
            ev.strengths,
            ev.improvements,
        ]
        if is_organizer:
            row.append(ev.confidential_notes)
        row.append(ev.updated_at.strftime('%Y-%m-%d %H:%M'))
        writer.writerow(row)

    return response
