"""Views for prize administration, official results and certificates."""

from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounts.models import User
from evaluations.models import Evaluation
from hackathons.models import Hackathon
from projects.models import Project
from teams.models import Team

from .forms import AssignWinnerForm, PrizeCategoryForm, PrizeForm, SponsorForm
from .models import (
    Certificate, LeaderboardEntry, Prize, PrizeAward, PrizeCategory, Sponsor,
)
from .pdf_generator import generate_certificate_pdf
from .permissions import (
    get_hackathon_for_organizer,
    get_prize_for_organizer,
    organizer_required,
)
from .services import (
    LeaderboardNotReady,
    compute_leaderboard,
    save_leaderboard,
    validate_prize_awards,
    validate_saved_leaderboard,
)


def _home_url(hackathon_id=None):
    url = reverse('prizes:home')
    return f'{url}?hackathon={hackathon_id}' if hackathon_id else url


@login_required
def prizes_home(request):
    """Show management to organizers and scoped read-only results to others."""
    if request.user.role == User.Role.ORGANIZER:
        return _prizes_organizer(request)
    if request.user.role == User.Role.JURY:
        evaluations = Evaluation.objects.filter(jury=request.user)
        hackathons = Hackathon.objects.filter(
            results_published=True,
            projects__evaluations__in=evaluations,
        ).distinct()
        return _prizes_readonly(request, hackathons, is_jury=True)
    if request.user.role == User.Role.PARTICIPANT:
        teams = Team.objects.filter(Q(members=request.user) | Q(leader=request.user))
        hackathons = Hackathon.objects.filter(teams__in=teams).distinct()
        return _prizes_readonly(request, hackathons, is_participant=True)
    raise PermissionDenied


def _prizes_organizer(request):
    hackathons = Hackathon.objects.filter(organizer=request.user).prefetch_related(
        'prizes'
    ).order_by('-created_at')
    selected_id = request.GET.get('hackathon')
    selected_hackathon = None
    if selected_id:
        selected_hackathon = get_hackathon_for_organizer(request.user, selected_id)
    elif hackathons.exists():
        selected_hackathon = hackathons.first()

    prizes = Prize.objects.none()
    leaderboard = LeaderboardEntry.objects.none()
    certificates = Certificate.objects.none()
    reviewers = User.objects.none()
    if selected_hackathon:
        prizes = Prize.objects.filter(hackathon=selected_hackathon).select_related(
            'category', 'sponsor', 'winning_project', 'winning_project__team'
        ).prefetch_related('awards__project__team')
        leaderboard = LeaderboardEntry.objects.filter(
            hackathon=selected_hackathon
        ).select_related('project', 'project__team').prefetch_related('project__prize_awards__prize')
        certificates = Certificate.objects.filter(
            hackathon=selected_hackathon
        ).select_related('recipient', 'prize').order_by('-issued_at')[:12]
        reviewers = User.objects.filter(
            evaluations__project__hackathon=selected_hackathon,
            role=User.Role.JURY,
        ).distinct().order_by('last_name', 'first_name', 'username')

    all_prizes = Prize.objects.filter(hackathon__organizer=request.user)
    stats = {
        'total_pool': sum((prize.amount or Decimal('0')) for prize in all_prizes),
        'total_prizes': all_prizes.count(),
        'available_count': all_prizes.filter(status=Prize.Status.AVAILABLE).count(),
        'awarded_count': all_prizes.filter(status=Prize.Status.AWARDED).count(),
        'completed_hackathons': hackathons.filter(status=Hackathon.Status.COMPLETED).count(),
    }
    return render(request, 'prizes/prizes_home.html', {
        'hackathons': hackathons,
        'selected_hackathon': selected_hackathon,
        'prizes': prizes,
        'leaderboard': leaderboard,
        'recent_certificates': certificates,
        'reviewers': reviewers,
        'categories': PrizeCategory.objects.all(),
        'stats': stats,
    })


def _prizes_readonly(request, hackathons, is_jury=False, is_participant=False):
    events = []
    for hackathon in hackathons.order_by('-created_at'):
        hackathon.prizes_display = list(
            Prize.objects.filter(hackathon=hackathon)
            .select_related('category', 'sponsor')
            .prefetch_related('awards__project__team')
        )
        hackathon.entries_display = []
        if hackathon.results_published:
            hackathon.entries_display = list(
                LeaderboardEntry.objects.filter(hackathon=hackathon)
                .select_related('project', 'project__team')
                .prefetch_related('project__prize_awards__prize')
                .order_by('rank', 'project__title')
            )
        events.append(hackathon)

    context = {
        'events': events,
        'is_jury': is_jury,
        'is_participant': is_participant,
    }
    if is_participant:
        context['my_certificates'] = Certificate.objects.filter(
            recipient=request.user,
            hackathon__results_published=True,
        ).select_related('hackathon', 'prize').order_by('-issued_at')
    return render(request, 'prizes/prizes_readonly.html', context)


@login_required
@organizer_required
def prize_create(request):
    initial = {}
    if request.method != 'POST' and request.GET.get('hackathon'):
        initial['hackathon'] = get_hackathon_for_organizer(
            request.user, request.GET['hackathon']
        ).pk
    form = PrizeForm(request.POST or None, organizer=request.user, initial=initial)
    if request.method == 'POST' and form.is_valid():
        prize = form.save()
        messages.success(request, _('Prize “%(name)s” created.') % {'name': prize.name})
        return redirect(_home_url(prize.hackathon_id))
    return render(request, 'prizes/prize_form.html', {
        'form': form, 'title': _('Create prize'), 'submit_label': _('Save prize'),
    })


@login_required
@organizer_required
def prize_edit(request, pk):
    prize = get_prize_for_organizer(request.user, pk)
    if prize.hackathon.results_published:
        messages.error(request, _('Unpublish results before changing this prize.'))
        return redirect('prizes:home')
    form = PrizeForm(request.POST or None, instance=prize, organizer=request.user)
    if request.method == 'POST' and form.is_valid():
        prize = form.save()
        messages.success(request, _('Prize “%(name)s” updated.') % {'name': prize.name})
        return redirect(_home_url(prize.hackathon_id))
    return render(request, 'prizes/prize_form.html', {
        'form': form, 'prize': prize, 'title': _('Edit prize'), 'submit_label': _('Update prize'),
    })


@login_required
@organizer_required
def prize_delete(request, pk):
    prize = get_prize_for_organizer(request.user, pk)
    if prize.hackathon.results_published or prize.awards.exists():
        messages.error(request, _('Remove the draft winner assignments before deleting this prize.'))
        return redirect(_home_url(prize.hackathon_id))
    if request.method == 'POST':
        name, hackathon_id = prize.name, prize.hackathon_id
        prize.delete()
        messages.success(request, _('Prize “%(name)s” deleted.') % {'name': name})
        return redirect(_home_url(hackathon_id))
    return render(request, 'prizes/prize_confirm_delete.html', {'prize': prize})


@login_required
@organizer_required
def prize_detail(request, pk):
    prize = get_prize_for_organizer(request.user, pk)
    return render(request, 'prizes/prize_detail.html', {
        'prize': prize,
    })


@login_required
@organizer_required
def prize_assign_winner(request, pk):
    prize = get_prize_for_organizer(request.user, pk)
    if prize.hackathon.results_published:
        messages.error(request, _('Unpublish results before changing prize assignments.'))
        return redirect('prizes:home')
    form = AssignWinnerForm(request.POST or None, prize=prize)
    if request.method == 'POST' and form.is_valid():
        projects = list(form.cleaned_data['projects'])
        rank_by_project = dict(LeaderboardEntry.objects.filter(
            hackathon=prize.hackathon,
            project__in=projects,
        ).values_list('project_id', 'rank'))
        projects.sort(key=lambda project: (rank_by_project.get(project.pk, 10**9), project.pk))
        with transaction.atomic():
            prize.awards.all().delete()
            PrizeAward.objects.bulk_create([
                PrizeAward(prize=prize, project=project, position=slot)
                for slot, project in enumerate(projects, start=1)
            ])
            prize.winning_project = projects[0] if projects else None
            prize.status = Prize.Status.ASSIGNED if projects else Prize.Status.AVAILABLE
            prize.save(update_fields=('winning_project', 'status', 'updated_at'))
        messages.success(request, _('Winner assignments for “%(name)s” saved.') % {'name': prize.name})
        return redirect(_home_url(prize.hackathon_id))
    return render(request, 'prizes/prize_assign_winner.html', {'form': form, 'prize': prize})


@login_required
@organizer_required
def sponsor_create(request):
    form = SponsorForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        sponsor = form.save(commit=False)
        sponsor.created_by = request.user
        sponsor.save()
        messages.success(request, _('Sponsor “%(name)s” created.') % {'name': sponsor.name})
        return redirect('prizes:sponsor_list')
    return render(request, 'prizes/sponsor_form.html', {'form': form, 'title': _('Add sponsor')})


@login_required
@organizer_required
def sponsor_list(request):
    sponsors = Sponsor.objects.all().order_by('name')
    return render(request, 'prizes/sponsor_list.html', {'sponsors': sponsors})


@login_required
@organizer_required
def sponsor_edit(request, pk):
    sponsor = get_object_or_404(Sponsor, pk=pk)
    if sponsor.created_by_id != request.user.pk or sponsor.prizes.exclude(
        hackathon__organizer=request.user
    ).exists() or sponsor.prizes.filter(hackathon__results_published=True).exists():
        raise PermissionDenied
    form = SponsorForm(request.POST or None, request.FILES or None, instance=sponsor)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, _('Sponsor updated.'))
        return redirect('prizes:sponsor_list')
    return render(request, 'prizes/sponsor_form.html', {
        'form': form, 'sponsor': sponsor, 'title': _('Edit sponsor'),
    })


@login_required
@organizer_required
def category_list(request):
    form = PrizeCategoryForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        category = form.save()
        messages.success(request, _('Category “%(name)s” created.') % {'name': category.name})
        return redirect('prizes:category_list')
    return render(request, 'prizes/category_list.html', {
        'categories': PrizeCategory.objects.all(), 'form': form,
    })


@login_required
@organizer_required
def category_edit(request, pk):
    category = get_object_or_404(PrizeCategory, pk=pk)
    if (category.prizes.exclude(hackathon__organizer=request.user).exists() or
            category.prizes.filter(hackathon__results_published=True).exists()):
        raise PermissionDenied
    form = PrizeCategoryForm(request.POST or None, instance=category)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, _('Category updated.'))
        return redirect('prizes:category_list')
    return render(request, 'prizes/category_form.html', {
        'category': category, 'form': form, 'title': _('Edit category'),
    })


@login_required
@organizer_required
def category_delete(request, pk):
    category = get_object_or_404(PrizeCategory, pk=pk)
    if (category.prizes.exclude(hackathon__organizer=request.user).exists() or
            category.prizes.filter(hackathon__results_published=True).exists()):
        raise PermissionDenied
    if request.method == 'POST':
        category.delete()  # Prize.category uses SET_NULL to preserve prize records.
        messages.success(request, _('Category deleted; existing prizes were preserved.'))
        return redirect('prizes:category_list')
    return render(request, 'prizes/category_confirm_delete.html', {'category': category})


@login_required
@organizer_required
def leaderboard_prepare(request, hackathon_pk):
    hackathon = get_hackathon_for_organizer(request.user, hackathon_pk)
    ranked = []
    readiness_error = None
    try:
        ranked = compute_leaderboard(hackathon)
    except LeaderboardNotReady as error:
        readiness_error = str(error)

    if request.method == 'POST' and request.POST.get('action') == 'save_leaderboard':
        if readiness_error:
            messages.error(request, readiness_error)
        else:
            try:
                save_leaderboard(hackathon, ranked)
            except LeaderboardNotReady as error:
                messages.error(request, str(error))
            else:
                messages.success(request, _('Draft ranking saved for review.'))
                return redirect('prizes:leaderboard_prepare', hackathon_pk=hackathon.pk)

    saved_entries = LeaderboardEntry.objects.filter(
        hackathon=hackathon
    ).select_related('project', 'project__team').order_by('rank', 'project__title')
    return render(request, 'prizes/leaderboard_prepare.html', {
        'hackathon': hackathon,
        'ranked': ranked,
        'readiness_error': readiness_error,
        'saved_entries': saved_entries,
        'can_publish': saved_entries.exists() and not readiness_error,
    })


@login_required
@organizer_required
def leaderboard_publish(request, hackathon_pk):
    hackathon = get_hackathon_for_organizer(request.user, hackathon_pk)
    entries = LeaderboardEntry.objects.filter(hackathon=hackathon).select_related(
        'project', 'project__team'
    ).order_by('rank', 'project__title')
    readiness_error = None
    if hackathon.results_published:
        readiness_error = _('Results are already published.')
    elif not entries.exists():
        readiness_error = _('Prepare and save a ranking before publishing results.')
    else:
        try:
            ranked = compute_leaderboard(hackathon)
            validate_saved_leaderboard(hackathon, ranked)
            validate_prize_awards(hackathon)
        except LeaderboardNotReady as error:
            readiness_error = str(error)

    if request.method == 'POST' and request.POST.get('confirm') == 'yes':
        if readiness_error:
            messages.error(request, readiness_error)
        else:
            with transaction.atomic():
                locked = Hackathon.objects.select_for_update().get(pk=hackathon.pk)
                if locked.organizer_id != request.user.pk:
                    raise PermissionDenied
                if locked.results_published:
                    messages.info(request, _('Results are already published.'))
                    return redirect(_home_url(hackathon.pk))
                locked.results_published = True
                locked.save(update_fields=('results_published', 'updated_at'))
                Prize.objects.filter(
                    hackathon=locked, status=Prize.Status.ASSIGNED
                ).update(status=Prize.Status.AWARDED, updated_at=timezone.now())
            messages.success(request, _('Official results have been published.'))
            return redirect(_home_url(hackathon.pk))
    return render(request, 'prizes/leaderboard_publish.html', {
        'hackathon': hackathon,
        'entries': entries,
        'readiness_error': readiness_error,
    })


@login_required
@organizer_required
def leaderboard_unpublish(request, hackathon_pk):
    hackathon = get_hackathon_for_organizer(request.user, hackathon_pk)
    if not hackathon.results_published:
        messages.info(request, _('Results are not currently published.'))
        return redirect('prizes:home')
    if Certificate.objects.filter(hackathon=hackathon).exists():
        messages.error(
            request,
            _('Results cannot be unpublished after certificates have been issued.'),
        )
        return redirect(_home_url(hackathon.pk))
    if request.method == 'POST' and request.POST.get('confirm') == 'yes':
        with transaction.atomic():
            locked = Hackathon.objects.select_for_update().get(pk=hackathon.pk)
            if locked.organizer_id != request.user.pk:
                raise PermissionDenied
            if Certificate.objects.filter(hackathon=locked).exists():
                messages.error(
                    request,
                    _('Results cannot be unpublished after certificates have been issued.'),
                )
                return redirect(_home_url(locked.pk))
            locked.results_published = False
            locked.save(update_fields=('results_published', 'updated_at'))
            Prize.objects.filter(
                hackathon=locked, status=Prize.Status.AWARDED
            ).update(status=Prize.Status.ASSIGNED, updated_at=timezone.now())
        messages.success(request, _('Publication removed; prize assignments remain drafts.'))
        return redirect(_home_url(hackathon.pk))
    return render(request, 'prizes/leaderboard_unpublish.html', {'hackathon': hackathon})


@login_required
@organizer_required
def certificate_generate(request, hackathon_pk):
    hackathon = get_hackathon_for_organizer(request.user, hackathon_pk)
    if not hackathon.results_published:
        messages.error(request, _('Publish official results before issuing certificates.'))
        return redirect('prizes:home')
    entries = LeaderboardEntry.objects.filter(hackathon=hackathon).select_related(
        'project', 'project__team'
    ).prefetch_related('project__team__members', 'project__prize_awards__prize').order_by('rank')
    if request.method == 'POST' and request.POST.get('confirm') == 'yes':
        generated = 0
        with transaction.atomic():
            locked = Hackathon.objects.select_for_update().get(pk=hackathon.pk)
            if locked.organizer_id != request.user.pk:
                raise PermissionDenied
            if not locked.results_published:
                messages.error(request, _('Publish official results before issuing certificates.'))
                return redirect(_home_url(locked.pk))
            for entry in entries:
                rank_type = (
                    Certificate.CertType.WINNER if entry.rank == 1 else
                    Certificate.CertType.FINALIST if entry.rank <= 3 else
                    Certificate.CertType.PARTICIPATION
                )
                team = entry.project.team
                recipients = User.objects.filter(role=User.Role.PARTICIPANT).filter(
                    Q(teams=team) | Q(led_teams=team)
                ).distinct()
                for recipient in recipients:
                    certificate, created = Certificate.objects.get_or_create(
                        recipient=recipient,
                        hackathon=hackathon,
                        cert_type=rank_type,
                        prize=None,
                        defaults={'rank': entry.rank},
                    )
                    generated += int(created)
                    for award in entry.project.prize_awards.filter(
                        prize__hackathon=hackathon,
                        prize__status=Prize.Status.AWARDED,
                    ).select_related('prize'):
                        certificate, created = Certificate.objects.get_or_create(
                            recipient=recipient,
                            hackathon=hackathon,
                            cert_type=Certificate.CertType.SPECIAL_AWARD,
                            prize=award.prize,
                            defaults={'rank': entry.rank},
                        )
                        generated += int(created)
        messages.success(
            request,
            _('%(count)d new certificate(s) issued. They can now be downloaded by their recipients.')
            % {'count': generated},
        )
        return redirect('prizes:certificate_list', hackathon_pk=hackathon.pk)
    return render(request, 'prizes/certificate_generate.html', {
        'hackathon': hackathon, 'entries': entries,
    })


@login_required
@organizer_required
def certificate_list(request, hackathon_pk):
    hackathon = get_hackathon_for_organizer(request.user, hackathon_pk)
    certificates = Certificate.objects.filter(hackathon=hackathon).select_related(
        'recipient', 'prize'
    ).order_by('-issued_at')
    return render(request, 'prizes/certificate_list.html', {
        'hackathon': hackathon, 'certificates': certificates,
    })


def _get_accessible_certificate(request, uid):
    certificate = get_object_or_404(
        Certificate.objects.select_related('recipient', 'hackathon', 'prize'), uid=uid
    )
    user = request.user
    if user.role == User.Role.PARTICIPANT:
        allowed = certificate.recipient_id == user.pk and certificate.hackathon.results_published
    elif user.role == User.Role.ORGANIZER:
        allowed = certificate.hackathon.organizer_id == user.pk
    else:
        allowed = False
    if not allowed:
        raise PermissionDenied
    return certificate


@login_required
def certificate_download(request, uid):
    certificate = _get_accessible_certificate(request, uid)
    try:
        pdf_bytes = generate_certificate_pdf(certificate)
    except (ImportError, ModuleNotFoundError):
        return HttpResponse(_('PDF generation is unavailable on this server.'), status=503)
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="certificate_{certificate.short_uid}.pdf"'
    response['Cache-Control'] = 'private, no-store'
    return response


@login_required
def certificate_preview(request, uid):
    certificate = _get_accessible_certificate(request, uid)
    return render(request, 'prizes/certificate_preview.html', {'cert': certificate})
