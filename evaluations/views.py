import json
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.db.models import Avg, Count, Q
from django.utils.translation import gettext_lazy as _

from .models import (
    Hackathon, Project, EvaluationCriteria,
    Evaluation, EvaluationScore, EvaluationComment,
)


# ---------------------------------------------------------------------------
# Main evaluations page
# ---------------------------------------------------------------------------

@login_required
def evaluations_home(request):
    user = request.user

    # ── Stats ──────────────────────────────────────────────────────────────
    total_projects = Project.objects.count()

    pending_evals = Evaluation.objects.filter(status=Evaluation.Status.PENDING).count()
    inprogress_evals = Evaluation.objects.filter(status=Evaluation.Status.IN_PROGRESS).count()
    completed_evals = Evaluation.objects.filter(status=Evaluation.Status.COMPLETED).count()

    # Average score across all completed evaluations
    completed_qs = Evaluation.objects.filter(status=Evaluation.Status.COMPLETED)
    avg_score = 0
    if completed_qs.exists():
        scores_sum = sum(e.total_score for e in completed_qs)
        avg_score = round(scores_sum / completed_qs.count())

    # ── Pending Reviews ────────────────────────────────────────────────────
    pending_list = Evaluation.objects.filter(
        status__in=[Evaluation.Status.PENDING, Evaluation.Status.IN_PROGRESS]
    ).select_related('project', 'project__hackathon', 'evaluator').order_by('project__hackathon__deadline')

    # ── Completed Reviews ──────────────────────────────────────────────────
    completed_list = Evaluation.objects.filter(
        status=Evaluation.Status.COMPLETED
    ).select_related('project', 'project__hackathon', 'evaluator')

    # annotate total_score lazily via property — already done in model

    # ── My Evaluations ─────────────────────────────────────────────────────
    my_evals = Evaluation.objects.filter(
        evaluator=user
    ).select_related('project', 'project__hackathon')

    # ── Recent feed (completed, latest first) ──────────────────────────────
    recent_feed = completed_list.order_by('-updated_at')[:8]

    # ── Score Distribution ─────────────────────────────────────────────────
    dist = {'exceptional': 0, 'proficient': 0, 'needs_work': 0, 'unsatisfactory': 0}
    for ev in completed_list:
        s = ev.total_score
        if s >= 85:
            dist['exceptional'] += 1
        elif s >= 70:
            dist['proficient'] += 1
        elif s >= 50:
            dist['needs_work'] += 1
        else:
            dist['unsatisfactory'] += 1

    total_done = completed_evals or 1  # avoid division by zero

    # ── Hackathons list for filter ──────────────────────────────────────────
    hackathons = Hackathon.objects.all()

    context = {
        'total_projects': total_projects,
        'pending_evals': pending_evals,
        'inprogress_evals': inprogress_evals,
        'completed_evals': completed_evals,
        'avg_score': avg_score,
        'pending_list': pending_list,
        'completed_list': completed_list,
        'my_evals': my_evals,
        'recent_feed': recent_feed,
        'dist': dist,
        'total_done': total_done,
        'hackathons': hackathons,
        'page': 'evaluations',
    }
    return render(request, 'evaluations.html', context)


# ---------------------------------------------------------------------------
# Create / load evaluation
# ---------------------------------------------------------------------------

@login_required
def evaluation_detail(request, project_id):
    """Return evaluation data + criteria as JSON (used by AJAX modal)."""
    project = get_object_or_404(Project, pk=project_id)
    hackathon = project.hackathon

    # Get or create the evaluation for this (project, evaluator) pair
    evaluation, _ = Evaluation.objects.get_or_create(
        project=project,
        evaluator=request.user,
        defaults={'status': Evaluation.Status.IN_PROGRESS}
    )

    # Criteria: hackathon-specific first, then global fallback
    criteria_qs = EvaluationCriteria.objects.filter(
        Q(hackathon=hackathon) | Q(hackathon__isnull=True)
    ).order_by('order', 'id')

    criteria_data = []
    for c in criteria_qs:
        existing = evaluation.scores.filter(criteria=c).first()
        criteria_data.append({
            'id': c.id,
            'name': c.name,
            'description': c.description,
            'weight': c.weight,
            'score': existing.score if existing else 5,
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

    # Update qualitative fields
    evaluation.strengths = body.get('strengths', '')
    evaluation.improvements = body.get('improvements', '')
    evaluation.confidential_notes = body.get('confidential_notes', '')

    # Save scores
    scores_data = body.get('scores', {})   # {criteria_id: score_value}
    for criteria_id_str, score_val in scores_data.items():
        try:
            criteria_id = int(criteria_id_str)
            score_val = max(0, min(10, int(score_val)))
            EvaluationScore.objects.update_or_create(
                evaluation=evaluation,
                criteria_id=criteria_id,
                defaults={'score': score_val},
            )
        except (ValueError, TypeError):
            continue

    # Update status
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
    evals = Evaluation.objects.filter(
        status=Evaluation.Status.COMPLETED
    ).select_related('project', 'project__hackathon', 'evaluator')

    if hackathon_id:
        evals = evals.filter(project__hackathon_id=hackathon_id)

    # Sort by total_score (computed property) — we sort in Python
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

    evals = Evaluation.objects.filter(
        status=Evaluation.Status.COMPLETED
    ).select_related('project', 'project__hackathon', 'evaluator').order_by('-updated_at')

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
