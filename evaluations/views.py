import json
import statistics
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST, require_GET
from django.db.models import Prefetch
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import (
    Hackathon, Project, EvaluationCriteria,
    Evaluation, EvaluationScore, EvaluationComment,
)

DEFAULT_CRITERIA = [
    (_("Innovation & Creativity"), _("Novelty, uniqueness of concept, and creative problem-solving approach."), 20, 1),
    (_("Technical Implementation"), _("Architecture soundness, code quality, stability, API integration depth."), 20, 2),
    (_("User Experience & Design Polish"), _("Interface polish, intuitive task flow, accessibility, and visual feedback."), 20, 3),
    (_("Impact & Relevance"), _("Problem clarity, market viability, direct target audience utility."), 20, 4),
    (_("Presentation & Documentation"), _("Clarity of demo video, pitch deck coherence, and README completeness."), 20, 5),
]


def ensure_default_criteria():
    if EvaluationCriteria.objects.filter(hackathon__isnull=True).exists():
        return
    EvaluationCriteria.objects.bulk_create([
        EvaluationCriteria(hackathon=None, name=str(name), description=str(desc), weight=weight, order=order)
        for name, desc, weight, order in DEFAULT_CRITERIA
    ])


def criteria_for_hackathon(hackathon):
    ensure_default_criteria()
    specific = EvaluationCriteria.objects.filter(hackathon=hackathon)
    if specific.exists():
        return specific.order_by('order', 'id')
    return EvaluationCriteria.objects.filter(hackathon__isnull=True).order_by('order', 'id')


def evaluation_queryset():
    return Evaluation.objects.select_related(
        'project', 'project__hackathon', 'evaluator'
    ).prefetch_related(
        Prefetch('scores', queryset=EvaluationScore.objects.select_related('criteria'))
    )


def build_catalog(user):
    evaluated_ids = set(
        Evaluation.objects.filter(evaluator=user).values_list('project_id', flat=True)
    )
    completed_ids = set(
        Evaluation.objects.filter(evaluator=user, status=Evaluation.Status.COMPLETED)
        .values_list('project_id', flat=True)
    )
    catalog = []
    for hackathon in Hackathon.objects.prefetch_related('projects'):
        projects = []
        for project in hackathon.projects.all():
            projects.append({
                'id': project.id,
                'name': project.name,
                'team_name': project.team_name,
                'started': project.id in evaluated_ids,
                'completed': project.id in completed_ids,
            })
        catalog.append({
            'id': hackathon.id,
            'name': hackathon.name,
            'track': hackathon.track or '',
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
def evaluations_home(request):
    user = request.user
    ensure_default_criteria()

    evals = list(evaluation_queryset().order_by('-updated_at'))
    pending_list = [e for e in evals if e.status in (Evaluation.Status.PENDING, Evaluation.Status.IN_PROGRESS)]
    completed_list = [e for e in evals if e.status == Evaluation.Status.COMPLETED]
    my_evals = [e for e in evals if e.evaluator_id == user.id]

    total_projects = Project.objects.count()
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
    dist_pct = {
        key: _pct(value, completed_evals) for key, value in dist.items()
    }

    next_hackathon = (
        Hackathon.objects.filter(is_active=True, deadline__isnull=False)
        .order_by('deadline')
        .first()
    )

    hackathons = list(Hackathon.objects.all())
    tracks = sorted({h.track for h in hackathons if h.track})

    criteria_count = EvaluationCriteria.objects.filter(hackathon__isnull=True).count() or 5
    for ev in evals:
        ev.scores_json = json.dumps([
            {'name': s.criteria.name, 'score': s.score}
            for s in ev.scores.all()
        ])
    for ev in my_evals:
        ev.scored_criteria = ev.scores.count()
        ev.criteria_total = criteria_count
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
        'now': timezone.now(),
        'page': 'evaluations',
    }
    return render(request, 'evaluations.html', context)


# ---------------------------------------------------------------------------
# Catalog (hackathons + projects for the start picker)
# ---------------------------------------------------------------------------

@login_required
@require_GET
def evaluation_catalog(request):
    return JsonResponse({'hackathons': build_catalog(request.user)})


# ---------------------------------------------------------------------------
# Start evaluation for a chosen project
# ---------------------------------------------------------------------------

@login_required
@require_POST
def evaluation_start(request):
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    project_id = body.get('project_id')
    if not project_id:
        return JsonResponse({'error': str(_('Please choose a project to evaluate.'))}, status=400)

    project = get_object_or_404(Project, pk=project_id)
    ensure_default_criteria()
    evaluation, created = Evaluation.objects.get_or_create(
        project=project,
        evaluator=request.user,
        defaults={'status': Evaluation.Status.IN_PROGRESS},
    )
    if evaluation.status == Evaluation.Status.PENDING:
        evaluation.status = Evaluation.Status.IN_PROGRESS
        evaluation.save(update_fields=['status', 'updated_at'])

    return JsonResponse({
        'success': True,
        'created': created,
        'evaluation_id': evaluation.id,
        'project_id': project.id,
        'status': evaluation.status,
    })


# ---------------------------------------------------------------------------
# Create / load evaluation
# ---------------------------------------------------------------------------

@login_required
def evaluation_detail(request, project_id):
    """Return evaluation data + criteria as JSON (used by AJAX modal)."""
    project = get_object_or_404(Project, pk=project_id)
    hackathon = project.hackathon

    evaluation, _ = Evaluation.objects.get_or_create(
        project=project,
        evaluator=request.user,
        defaults={'status': Evaluation.Status.IN_PROGRESS},
    )
    if evaluation.status == Evaluation.Status.PENDING:
        evaluation.status = Evaluation.Status.IN_PROGRESS
        evaluation.save(update_fields=['status', 'updated_at'])

    criteria_qs = criteria_for_hackathon(hackathon)
    existing_scores = {s.criteria_id: s.score for s in evaluation.scores.all()}

    criteria_data = []
    for c in criteria_qs:
        criteria_data.append({
            'id': c.id,
            'name': c.name,
            'description': c.description,
            'weight': c.weight,
            'score': existing_scores.get(c.id, 5),
        })

    data = {
        'project_id': project.id,
        'project_name': project.name,
        'team_name': project.team_name,
        'hackathon': hackathon.name,
        'github_url': project.github_url,
        'demo_url': project.demo_url,
        'evaluation_id': evaluation.id,
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
@require_POST
def evaluation_submit(request):
    """Save or submit an evaluation (JSON body)."""
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    evaluation_id = body.get('evaluation_id')
    action = body.get('action', 'draft')  # 'draft' | 'submit'

    evaluation = get_object_or_404(Evaluation, pk=evaluation_id, evaluator=request.user)

    evaluation.strengths = body.get('strengths', '')
    evaluation.improvements = body.get('improvements', '')
    evaluation.confidential_notes = body.get('confidential_notes', '')

    allowed_ids = set(criteria_for_hackathon(evaluation.project.hackathon).values_list('id', flat=True))
    scores_data = body.get('scores', {})
    for criteria_id_str, score_val in scores_data.items():
        try:
            criteria_id = int(criteria_id_str)
            if criteria_id not in allowed_ids:
                continue
            score_val = max(0, min(10, int(score_val)))
            EvaluationScore.objects.update_or_create(
                evaluation=evaluation,
                criteria_id=criteria_id,
                defaults={'score': score_val},
            )
        except (ValueError, TypeError):
            continue

    if action == 'submit':
        evaluation.status = Evaluation.Status.COMPLETED
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
@require_POST
def evaluation_add_comment(request, evaluation_id):
    """Add a comment to an evaluation."""
    evaluation = get_object_or_404(Evaluation, pk=evaluation_id)
    text = request.POST.get('text', '').strip()
    if not text:
        return JsonResponse({'error': 'Comment cannot be empty.'}, status=400)

    comment = EvaluationComment.objects.create(
        evaluation=evaluation,
        author=request.user,
        text=text,
    )
    return JsonResponse({
        'success': True,
        'comment': {
            'id': comment.id,
            'author': comment.author.get_full_name() or comment.author.username,
            'text': comment.text,
            'created_at': comment.created_at.strftime('%b %d, %Y • %H:%M'),
        }
    })


# ---------------------------------------------------------------------------
# Leaderboard / Results
# ---------------------------------------------------------------------------

@login_required
def results_leaderboard(request):
    """Public leaderboard: completed evaluations sorted by total_score desc."""
    hackathon_id = request.GET.get('hackathon', '')
    evals = evaluation_queryset().filter(status=Evaluation.Status.COMPLETED)

    if hackathon_id:
        evals = evals.filter(project__hackathon_id=hackathon_id)

    ranked = sorted(evals, key=lambda e: e.total_score, reverse=True)

    hackathons = Hackathon.objects.all()
    context = {
        'ranked': ranked,
        'hackathons': hackathons,
        'selected_hackathon': hackathon_id,
        'page': 'evaluations',
    }
    return render(request, 'evaluations_results.html', context)


# ---------------------------------------------------------------------------
# Export CSV
# ---------------------------------------------------------------------------

@login_required
def export_csv(request):
    """Export all completed evaluations as a CSV file."""
    import csv
    from django.http import HttpResponse

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="evaluations_export.csv"'

    writer = csv.writer(response)
    writer.writerow([
        'Project', 'Team', 'Hackathon', 'Evaluator',
        'Total Score', 'Status', 'Strengths', 'Improvements', 'Notes', 'Date',
    ])

    evals = evaluation_queryset().filter(
        status=Evaluation.Status.COMPLETED
    ).order_by('-updated_at')

    for ev in evals:
        evaluator_name = ev.evaluator.get_full_name() if ev.evaluator else ''
        writer.writerow([
            ev.project.name,
            ev.project.team_name,
            ev.project.hackathon.name,
            evaluator_name,
            ev.total_score,
            ev.get_status_display(),
            ev.strengths,
            ev.improvements,
            ev.confidential_notes,
            ev.updated_at.strftime('%Y-%m-%d %H:%M'),
        ])

    return response
