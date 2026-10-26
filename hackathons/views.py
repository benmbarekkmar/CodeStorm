import json
import logging
import time

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from . import ai
from accounts.models import User
from teams.models import Team
from .forms import ChallengeForm, HackathonForm
from .models import Challenge, Hackathon
from .permissions import organizer_required

logger = logging.getLogger(__name__)


def visible_hackathons(user):
    """Tout le monde voit les hackathons publiés ; l'organisateur voit aussi ses brouillons."""
    return Hackathon.objects.select_related('organizer').filter(
        ~Q(status=Hackathon.Status.DRAFT) | Q(organizer=user)
    )


# ------------------------------------------------------------------ HACKATHONS
@login_required
def hackathon_list(request):
    base = visible_hackathons(request.user)
    now = timezone.now()
    pub = Hackathon.Status.PUBLISHED

    stats = base.aggregate(
        total=Count('id'),
        upcoming=Count('id', filter=Q(status=pub, start_date__gt=now)),
        ongoing=Count('id', filter=Q(status=pub, start_date__lte=now, end_date__gte=now)),
        completed=Count('id', filter=Q(status=pub, end_date__lt=now)),
    )

    qs = base
    q = request.GET.get('q', '').strip()
    category = request.GET.get('category', '')
    phase = request.GET.get('phase', '')
    scope = request.GET.get('scope', '')

    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))
    if category in Hackathon.Category.values:
        qs = qs.filter(category=category)
    if phase == 'upcoming':
        qs = qs.filter(status=pub, start_date__gt=now)
    elif phase == 'ongoing':
        qs = qs.filter(status=pub, start_date__lte=now, end_date__gte=now)
    elif phase == 'completed':
        qs = qs.filter(status=pub, end_date__lt=now)
    elif phase == 'draft' and request.user.role == 'organizer':
        qs = qs.filter(status=Hackathon.Status.DRAFT, organizer=request.user)
    if scope == 'mine':
        qs = qs.filter(organizer=request.user)

    page = Paginator(qs, 6).get_page(request.GET.get('page'))
    return render(request, 'hackathons/hackathon_list.html', {
        'page': page, 'stats': stats, 'categories': Hackathon.Category.choices,
        'filters': {'q': q, 'category': category, 'phase': phase, 'scope': scope},
    })


@login_required
def hackathon_detail(request, pk):
    hackathon = get_object_or_404(visible_hackathons(request.user), pk=pk)
    is_owner = hackathon.organizer_id == request.user.id
    challenges = hackathon.challenges.all()
    if not is_owner:
        challenges = challenges.filter(is_published=True)
    my_team = None
    if request.user.role == User.Role.PARTICIPANT:
        my_team = (
            Team.objects
            .filter(hackathon=hackathon, memberships__user=request.user)
            .select_related('hackathon')
            .first()
        )
    team_count = hackathon.teams.count()
    return render(request, 'hackathons/hackathon_detail.html', {
        'hackathon': hackathon, 'challenges': challenges, 'is_owner': is_owner,
        'my_team': my_team, 'team_count': team_count,
        'is_participant': request.user.role == User.Role.PARTICIPANT,
    })


@organizer_required
def hackathon_create(request):
    form = HackathonForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        hackathon = form.save(commit=False)
        hackathon.organizer = request.user
        hackathon.save()
        messages.success(request, _('Hackathon created as a draft. Add challenges, then publish it.'))
        return redirect('hackathons:detail', pk=hackathon.pk)
    return render(request, 'hackathons/form.html', {
        'form': form, 'title': _('Create a hackathon'), 'show_ai': True,
        'cancel_url': 'hackathons:home',
    })


@organizer_required
def hackathon_update(request, pk):
    hackathon = get_object_or_404(Hackathon, pk=pk, organizer=request.user)
    form = HackathonForm(request.POST or None, request.FILES or None, instance=hackathon)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, _('Hackathon updated.'))
        return redirect('hackathons:detail', pk=hackathon.pk)
    return render(request, 'hackathons/form.html', {
        'form': form, 'title': _('Edit hackathon'), 'show_ai': True,
        'cancel_url': 'hackathons:home',
    })


@organizer_required
def hackathon_delete(request, pk):
    hackathon = get_object_or_404(Hackathon, pk=pk, organizer=request.user)
    if request.method == 'POST':
        hackathon.delete()
        messages.success(request, _('Hackathon deleted.'))
        return redirect('hackathons:home')
    return render(request, 'hackathons/confirm_delete.html', {'object': hackathon})


@organizer_required
@require_POST
def hackathon_toggle_publish(request, pk):
    hackathon = get_object_or_404(Hackathon, pk=pk, organizer=request.user)
    if hackathon.status == Hackathon.Status.CANCELLED:
        messages.error(request, _('A cancelled hackathon cannot be published.'))
    elif hackathon.status == Hackathon.Status.DRAFT:
        hackathon.status = Hackathon.Status.PUBLISHED
        hackathon.save(update_fields=['status', 'updated_at'])
        messages.success(request, _('Hackathon published.'))
    else:
        hackathon.status = Hackathon.Status.DRAFT
        hackathon.save(update_fields=['status', 'updated_at'])
        messages.success(request, _('Hackathon moved back to draft.'))
    return redirect('hackathons:detail', pk=pk)


@organizer_required
@require_POST
def hackathon_cancel(request, pk):
    hackathon = get_object_or_404(Hackathon, pk=pk, organizer=request.user)
    hackathon.status = Hackathon.Status.CANCELLED
    hackathon.save(update_fields=['status', 'updated_at'])
    messages.success(request, _('Hackathon cancelled.'))
    return redirect('hackathons:detail', pk=pk)


# ------------------------------------------------------------------ CHALLENGES
@organizer_required
def challenge_create(request, hackathon_pk):
    hackathon = get_object_or_404(Hackathon, pk=hackathon_pk, organizer=request.user)
    form = ChallengeForm(request.POST or None, instance=Challenge(hackathon=hackathon))
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, _('Challenge added.'))
        return redirect('hackathons:detail', pk=hackathon.pk)
    return render(request, 'hackathons/form.html', {
        'form': form, 'title': _('Add a challenge'), 'show_ai': False,
        'cancel_url': 'hackathons:home',
    })


@organizer_required
def challenge_update(request, pk):
    challenge = get_object_or_404(Challenge, pk=pk, hackathon__organizer=request.user)
    form = ChallengeForm(request.POST or None, instance=challenge)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, _('Challenge updated.'))
        return redirect('hackathons:detail', pk=challenge.hackathon_id)
    return render(request, 'hackathons/form.html', {
        'form': form, 'title': _('Edit challenge'), 'show_ai': False,
        'cancel_url': 'hackathons:home',
    })


@organizer_required
def challenge_delete(request, pk):
    challenge = get_object_or_404(Challenge, pk=pk, hackathon__organizer=request.user)
    hackathon_id = challenge.hackathon_id
    if request.method == 'POST':
        challenge.delete()
        messages.success(request, _('Challenge deleted.'))
        return redirect('hackathons:detail', pk=hackathon_id)
    return render(request, 'hackathons/confirm_delete.html', {'object': challenge})


@organizer_required
@require_POST
def challenge_toggle_publish(request, pk):
    challenge = get_object_or_404(Challenge, pk=pk, hackathon__organizer=request.user)
    challenge.is_published = not challenge.is_published
    challenge.published_at = timezone.now() if challenge.is_published else None
    challenge.save(update_fields=['is_published', 'published_at'])
    messages.success(request, _('Challenge published.') if challenge.is_published
                     else _('Challenge unpublished.'))
    return redirect('hackathons:detail', pk=challenge.hackathon_id)


# ------------------------------------------------------------------ IA
@organizer_required
@require_POST
def generate_description(request):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': _('Invalid request.')}, status=400)

    title = str(data.get('title', '')).strip()[:150]
    category = str(data.get('category', '')).strip()[:50]
    if not title and not category:
        return JsonResponse({'error': _('Please enter a title or choose a category first.')}, status=400)

    started = time.perf_counter()
    try:
        if title:
            text = ai.generate_description(title, category, request.user.language)
            payload = {'description': text, 'elapsed_ms': None}
        else:
            payload = ai.generate_title_and_description(category, request.user.language)
    except ai.AIServiceError as exc:
        return JsonResponse({'error': str(exc)}, status=502)
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    payload['elapsed_ms'] = elapsed_ms
    logger.info('AI description generated in %s ms (title=%r)', elapsed_ms, title)
    return JsonResponse(payload)