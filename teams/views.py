from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from hackathons.models import Hackathon
from teams import matching
from teams.forms import InviteMemberForm, TeamForm
from teams.models import JoinRequest, Team, TeamInvitation, TeamMembership

User = get_user_model()

PARTICIPANT = User.Role.PARTICIPANT
ORGANIZER = User.Role.ORGANIZER
JURY = User.Role.JURY


# --------------------------------------------------------------------------
# Permission helpers
# --------------------------------------------------------------------------
def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.user.role not in roles:
                messages.error(request, _('You do not have permission to perform this action.'))
                return redirect('teams:home')
            return view(request, *args, **kwargs)
        return wrapper
    return decorator


def participant_required(view):
    return role_required(PARTICIPANT)(view)


def _team_or_404(pk):
    return get_object_or_404(Team.objects.select_related('hackathon', 'captain'), pk=pk)


def _forbidden(request, team):
    messages.error(request, _('You do not have permission to manage this team.'))
    return redirect('teams:detail', pk=team.pk)


# --------------------------------------------------------------------------
# Business operations (server-side validated, transactional)
# --------------------------------------------------------------------------
@transaction.atomic
def _accept_invitation(invitation, user):
    invitation = TeamInvitation.objects.select_for_update().get(pk=invitation.pk)
    if invitation.invitee_id != user.pk:
        return False, _('You cannot respond to an invitation addressed to another user.')
    invitation.mark_expired_if_due()
    if invitation.status != TeamInvitation.Status.PENDING:
        return False, _('This invitation is no longer valid.')
    team = Team.objects.select_for_update().get(pk=invitation.team_id)
    # Being invited waives only the join-request opt-ins (visibility,
    # allow_join_requests); capacity, the hackathon registration window,
    # duplicate memberships and one team per hackathon still apply.
    ok, reason = team.is_joinable(user, via_invitation=True)
    if not ok:
        if team.is_full():
            invitation.status = TeamInvitation.Status.EXPIRED
            invitation.responded_at = timezone.now()
            invitation.save(update_fields=['status', 'responded_at'])
            return False, _('This team is already full.')
        if team.has_member(user):
            invitation.status = TeamInvitation.Status.ACCEPTED
            invitation.responded_at = timezone.now()
            invitation.save(update_fields=['status', 'responded_at'])
        return False, reason

    team.add_member(user)
    invitation.status = TeamInvitation.Status.ACCEPTED
    invitation.responded_at = timezone.now()
    invitation.save(update_fields=['status', 'responded_at'])
    return True, ''


@transaction.atomic
def _approve_join_request(request_obj, captain):
    request_obj = JoinRequest.objects.select_for_update().get(pk=request_obj.pk)
    if request_obj.status != JoinRequest.Status.PENDING:
        return False, _('This request has already been processed.')
    team = Team.objects.select_for_update().get(pk=request_obj.team_id)
    if not team.is_captain(captain):
        return False, _('Only the captain can review requests.')
    user = request_obj.user
    # Re-validate every eligibility rule at approval time (status, visibility,
    # allow_join_requests, hackathon window, capacity, duplicate team).
    ok, reason = team.is_joinable(user)
    if not ok:
        if team.has_member(user):
            request_obj.status = JoinRequest.Status.ACCEPTED
            request_obj.responded_at = timezone.now()
            request_obj.save(update_fields=['status', 'responded_at'])
        return False, reason
    team.add_member(user)
    request_obj.status = JoinRequest.Status.ACCEPTED
    request_obj.responded_at = timezone.now()
    request_obj.save(update_fields=['status', 'responded_at'])
    return True, ''


def _visible_teams(user):
    """Queryset of teams the user may see in the list, per role."""
    if user.role == ORGANIZER:
        return Team.objects.filter(
            hackathon__organizer=user,
        ).select_related('hackathon', 'captain')
    if user.role == JURY:
        return Team.objects.filter(
            visibility=Team.Visibility.PUBLIC,
        ).select_related('hackathon', 'captain')
    return Team.objects.select_related('hackathon', 'captain')


def _apply_filters(qs, request):
    q = request.GET.get('q', '').strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q))
    hackathon = request.GET.get('hackathon', '').strip()
    if hackathon and hackathon != 'all':
        qs = qs.filter(hackathon_id=hackathon)
    status = request.GET.get('availability', '').strip()
    if status == 'recruiting':
        qs = qs.filter(status=Team.Status.ACTIVE, visibility=Team.Visibility.PUBLIC)
    elif status == 'full':
        qs = qs.filter(
            status=Team.Status.ACTIVE, visibility=Team.Visibility.PUBLIC,
        ).annotate(m=Count('memberships')).filter(m__gte=F('max_members'))
    return qs


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------
def _team_create_form(request):
    """Create form for the participant modal, pre-filled with ?hackathon=.

    The « Participer » action on a hackathon page lands here, so the team is
    created for that hackathon instead of asking for it a second time.
    """
    if request.user.role != PARTICIPANT:
        return None
    selected = request.GET.get('hackathon', '').strip()
    return TeamForm(initial={'hackathon': selected} if selected.isdigit() else {})


@login_required
def team_list(request):
    user = request.user
    teams_qs = _visible_teams(user)
    teams_qs = _apply_filters(teams_qs, request)

    membership = request.GET.get('membership', 'all') if user.role == PARTICIPANT else 'all'

    if user.role == PARTICIPANT:
        member_teams = list(teams_qs.filter(memberships__user=user).distinct())
        if membership == 'created':
            my_teams = [t for t in member_teams if t.created_by_id == user.pk]
            discover = []
        elif membership == 'member':
            my_teams = member_teams
            discover = []
        else:
            my_teams = member_teams
            my_ids = {t.pk for t in my_teams}
            discover = [t for t in teams_qs if t.pk not in my_ids and t.visibility == Team.Visibility.PUBLIC]
        browse = []
    else:
        # Organizer and jury browse a read-only list of the teams they may view.
        my_teams, discover = [], []
        browse = list(teams_qs)

    # Organizer statistics
    stats = {}
    if user.role == ORGANIZER:
        base = _visible_teams(user)
        stats = {
            'total': base.count(),
            'full': sum(1 for t in base if t.is_full()),
            'incomplete': sum(1 for t in base if not t.is_full()),
            'members': base.aggregate(m=Count('memberships'))['m'],
        }

    invitations = TeamInvitation.objects.filter(invitee=user, status=TeamInvitation.Status.PENDING)
    pending_invites = invitations.count()

    # Join-request state: per-team pending counts (for captains) and the
    # current user's own pending/member flags, used by the team cards.
    captain_pending = dict(
        JoinRequest.objects.filter(
            status=JoinRequest.Status.PENDING, team__captain=user,
        ).values_list('team_id').annotate(c=Count('id'))
    )
    my_pending_ids = set(
        JoinRequest.objects.filter(
            status=JoinRequest.Status.PENDING, user=user,
        ).values_list('team_id', flat=True)
    )
    member_team_ids = set(
        TeamMembership.objects.filter(user=user).values_list('team_id', flat=True)
    )
    for group in (my_teams, discover, browse):
        for t in group:
            t.pending_requests = captain_pending.get(t.pk, 0)
            t.my_pending = t.pk in my_pending_ids
            t.user_is_member = t.pk in member_team_ids
    # Join eligibility comes from Team.is_joinable(), the very rule the POST
    # endpoints run, so a card never shows a button the server would refuse.
    # Two batched queries feed the pre-computed hints: no query per card.
    if user.role == PARTICIPANT and discover:
        counts = dict(
            Team.objects.filter(pk__in=[t.pk for t in discover])
            .values_list('pk').annotate(c=Count('memberships'))
        )
        my_hackathon_ids = set(
            TeamMembership.objects.filter(user=user)
            .values_list('team__hackathon_id', flat=True)
        )
        for t in discover:
            t.can_join, t.join_reason = t.is_joinable(
                user,
                member_count=counts.get(t.pk, 0),
                user_is_member=t.pk in member_team_ids,
                user_has_team_in_hackathon=t.hackathon_id in my_hackathon_ids,
            )
    pending_join_requests = sum(captain_pending.values())

    context = {
        'my_teams': my_teams,
        'discover': discover,
        'browse': browse,
        'stats': stats,
        'pending_invites': pending_invites,
        'pending_join_requests': pending_join_requests,
        'hackathons': Hackathon.objects.all(),
        'create_form': _team_create_form(request),
        'is_organizer': user.role == ORGANIZER,
        'is_participant': user.role == PARTICIPANT,
        'is_jury': user.role == JURY,
        'q': request.GET.get('q', ''),
        'sel_hackathon': request.GET.get('hackathon', 'all'),
        'sel_availability': request.GET.get('availability', 'all'),
        'sel_membership': membership,
    }
    return render(request, 'teams/team_list.html', context)


@login_required
@participant_required
def team_create(request):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    form = TeamForm(request.POST or None)
    if request.method == 'GET':
        prefill = request.GET.get('hackathon', '').strip()
        if prefill.isdigit():
            form.initial['hackathon'] = prefill
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            team = form.save(commit=False)
            team.created_by = request.user
            team.captain = request.user
            if team.max_members is None:
                team.max_members = 5
            team.save()
            team.add_member(request.user, role=TeamMembership.Role.CAPTAIN)
        messages.success(request, _('Team created. You are now its captain.'))
        if is_ajax:
            return JsonResponse({'redirect': reverse('teams:detail', args=[team.pk])})
        return redirect('teams:detail', pk=team.pk)
    if is_ajax:
        return render(request, 'teams/_create_form.html', {'form': form})
    return render(request, 'teams/team_form.html', {'form': form, 'mode': 'create'})


@login_required
def team_detail(request, pk):
    team = _team_or_404(pk)
    user = request.user
    memberships = team.memberships.select_related('user').order_by('role', 'joined_at')
    join_requests = None
    incoming_invites = None
    if team.is_captain(user):
        join_requests = team.join_requests.filter(status=JoinRequest.Status.PENDING).select_related('user')
        incoming_invites = team.invitations.filter(status=TeamInvitation.Status.PENDING)

    can_view_private = user.role != JURY and (team.is_captain(user) or team.has_member(user) or user.role == ORGANIZER)
    if team.visibility == Team.Visibility.PRIVATE and not can_view_private:
        messages.info(request, _('This team is private.'))
        return redirect('teams:home')

    invite_form = InviteMemberForm(team=team) if team.is_captain(user) else None
    edit_form = TeamForm(instance=team) if team.is_captain(user) else None
    joinable, join_reason = team.is_joinable(user)
    pending_request = None
    if user.role == PARTICIPANT and not team.is_captain(user):
        pending_request = team.join_requests.filter(
            user=user, status=JoinRequest.Status.PENDING,
        ).first()

    context = {
        'team': team,
        'memberships': memberships,
        'join_requests': join_requests,
        'pending_invites': incoming_invites,
        'invite_form': invite_form,
        'edit_form': edit_form,
        'is_captain': team.is_captain(user),
        'is_member': team.has_member(user),
        'is_participant': user.role == PARTICIPANT,
        'can_join': joinable and user.role == PARTICIPANT,
        'join_reason': join_reason,
        'pending_request': pending_request,
        'can_edit': team.is_captain(user),
        'remaining': team.remaining_slots(),
    }
    return render(request, 'teams/team_detail.html', context)


@login_required
def team_edit(request, pk):
    team = _team_or_404(pk)
    if not team.is_captain(request.user):
        return _forbidden(request, team)
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    form = TeamForm(request.POST or None, instance=team)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, _('Team updated.'))
        if is_ajax:
            return JsonResponse({'redirect': reverse('teams:detail', args=[team.pk])})
        return redirect('teams:detail', pk=team.pk)
    if is_ajax:
        return render(request, 'teams/_edit_form.html', {'form': form, 'team': team})
    return render(request, 'teams/team_form.html', {'form': form, 'mode': 'edit', 'team': team})


@login_required
@participant_required
def team_leave(request, pk):
    if request.method != 'POST':
        return redirect('teams:detail', pk=pk)
    team = _team_or_404(pk)
    membership = team.membership_for(request.user)
    if not membership:
        messages.error(request, _('You are not a member of this team.'))
        return redirect('teams:detail', pk=team.pk)
    if team.is_captain(request.user):
        if team.member_count() > 1:
            messages.error(request, _('Transfer the captaincy before leaving the team.'))
        else:
            messages.error(request, _('You are the last member; disband the team instead.'))
        return redirect('teams:detail', pk=team.pk)
    membership.delete()
    messages.success(request, _('You left "%(team)s".') % {'team': team.name})
    return redirect('teams:home')


@login_required
def team_remove_member(request, pk, membership_id):
    if request.method != 'POST':
        return redirect('teams:detail', pk=pk)
    team = _team_or_404(pk)
    if not team.is_captain(request.user):
        return _forbidden(request, team)
    membership = get_object_or_404(team.memberships, pk=membership_id)
    if membership.user_id == team.captain_id:
        messages.error(request, _('The captain cannot be removed. Transfer leadership first.'))
        return redirect('teams:detail', pk=team.pk)
    membership.delete()
    messages.success(request, _('Member removed.'))
    return redirect('teams:detail', pk=team.pk)


@login_required
def team_transfer_captain(request, pk):
    if request.method != 'POST':
        return redirect('teams:detail', pk=pk)
    team = _team_or_404(pk)
    if not team.is_captain(request.user):
        return _forbidden(request, team)
    new_id = request.POST.get('new_captain')
    new_membership = team.memberships.filter(user_id=new_id).first()
    if not new_membership:
        messages.error(request, _('Select a current member as the new captain.'))
        return redirect('teams:detail', pk=team.pk)
    with transaction.atomic():
        old = team.membership_for(request.user)
        if old:
            old.role = TeamMembership.Role.MEMBER
            old.save(update_fields=['role'])
        new_membership.role = TeamMembership.Role.CAPTAIN
        new_membership.save(update_fields=['role'])
        team.captain = new_membership.user
        team.save(update_fields=['captain'])
    messages.success(request, _('Captaincy transferred.'))
    return redirect('teams:detail', pk=team.pk)


@login_required
def team_disband(request, pk):
    team = _team_or_404(pk)
    if not team.is_captain(request.user):
        return _forbidden(request, team)
    if request.method != 'POST':
        return render(request, 'teams/team_confirm.html', {'team': team, 'action': 'disband'})
    team.delete()
    messages.success(request, _('Team disbanded.'))
    return redirect('teams:home')


# --- Invitations ---------------------------------------------------------
@login_required
def invitations(request):
    user = request.user
    received = (
        TeamInvitation.objects.filter(invitee=user)
        .select_related('team', 'inviter').order_by('-created_at')
    )
    sent = (
        TeamInvitation.objects.filter(inviter=user)
        .select_related('team', 'invitee').order_by('-created_at')
    ) if user.role == PARTICIPANT else TeamInvitation.objects.none()
    for inv in received:
        inv.mark_expired_if_due()
    return render(request, 'teams/team_invitations.html', {'received': received, 'sent': sent})


@login_required
def team_send_invitation(request, pk):
    if request.method != 'POST':
        return redirect('teams:detail', pk=pk)
    team = _team_or_404(pk)
    if not team.is_captain(request.user):
        return _forbidden(request, team)
    form = InviteMemberForm(request.POST, team=team)
    if form.is_valid():
        try:
            with transaction.atomic():
                TeamInvitation.objects.create(
                    team=team,
                    inviter=request.user,
                    invitee=form.cleaned_data['invitee'],
                    message=form.cleaned_data['message'],
                    expires_at=timezone.now() + timezone.timedelta(days=7),
                )
            messages.success(request, _('Invitation sent.'))
        except IntegrityError:
            messages.error(request, _('An invitation is already pending for this user.'))
    else:
        for err in form.errors.get('invitee', []):
            messages.error(request, err)
    return redirect('teams:detail', pk=team.pk)


def _respond_invitation(request, invitation_id, accept):
    if request.method != 'POST':
        return redirect('teams:invitations')
    with transaction.atomic():
        invitation = get_object_or_404(
            TeamInvitation.objects.select_for_update(), pk=invitation_id,
        )
        if accept:
            ok, reason = _accept_invitation(invitation, request.user)
            if ok:
                messages.success(request, _('You joined "%(team)s".') % {'team': invitation.team.name})
                return redirect('teams:detail', pk=invitation.team.pk)
            messages.error(request, reason)
        else:
            if invitation.invitee_id != request.user.pk:
                messages.error(request, _('You cannot respond to an invitation addressed to another user.'))
            elif invitation.status == TeamInvitation.Status.PENDING:
                invitation.status = TeamInvitation.Status.DECLINED
                invitation.responded_at = timezone.now()
                invitation.save(update_fields=['status', 'responded_at'])
                messages.info(request, _('Invitation declined.'))
            else:
                messages.error(request, _('This invitation is no longer valid.'))
        return redirect('teams:invitations')


@login_required
def invitation_accept(request, invitation_id):
    return _respond_invitation(request, invitation_id, accept=True)


@login_required
def invitation_decline(request, invitation_id):
    return _respond_invitation(request, invitation_id, accept=False)


@login_required
def invitation_cancel(request, invitation_id):
    if request.method != 'POST':
        return redirect('teams:invitations')
    invitation = get_object_or_404(TeamInvitation, pk=invitation_id)
    if invitation.inviter_id != request.user.pk:
        messages.error(request, _('You can only cancel invitations you sent.'))
        return redirect('teams:invitations')
    if invitation.status == TeamInvitation.Status.PENDING:
        invitation.status = TeamInvitation.Status.CANCELLED
        invitation.responded_at = timezone.now()
        invitation.save(update_fields=['status', 'responded_at'])
        messages.info(request, _('Invitation cancelled.'))
    return redirect('teams:invitations')


# --- Join requests -----------------------------------------------------
@login_required
@participant_required
def team_request_join(request, pk):
    if request.method != 'POST':
        return redirect('teams:detail', pk=pk)
    team = _team_or_404(pk)
    ok, reason = team.is_joinable(request.user)
    if not ok:
        messages.error(request, reason)
        return redirect('teams:detail', pk=team.pk)
    try:
        with transaction.atomic():
            _instance, created = JoinRequest.objects.get_or_create(
                team=team, user=request.user, status=JoinRequest.Status.PENDING,
            )
    except IntegrityError:
        created = False
    messages.success(request, _('Your join request was submitted.') if created
                     else _('You already requested to join this team.'))
    return redirect('teams:detail', pk=team.pk)


@login_required
def join_request_respond(request, request_id):
    if request.method != 'POST':
        return redirect('teams:home')
    join_request = get_object_or_404(JoinRequest.objects.select_related('team'), pk=request_id)
    team = join_request.team
    if not team.is_captain(request.user):
        return _forbidden(request, team)
    accept = request.POST.get('action') == 'accept'
    if accept:
        ok, reason = _approve_join_request(join_request, request.user)
        (messages.success if ok else messages.error)(
            request, _('Request accepted.') if ok else reason,
        )
    else:
        if join_request.status == JoinRequest.Status.PENDING:
            join_request.status = JoinRequest.Status.REJECTED
            join_request.responded_at = timezone.now()
            join_request.save(update_fields=['status', 'responded_at'])
            messages.info(request, _('Request rejected.'))
    return redirect('teams:detail', pk=team.pk)


@login_required
@participant_required
def join_request_cancel(request, request_id):
    if request.method != 'POST':
        return redirect('teams:home')
    join_request = get_object_or_404(JoinRequest, pk=request_id)
    if join_request.user_id != request.user.pk:
        messages.error(request, _('You can only cancel your own requests.'))
        return redirect('teams:home')
    if join_request.status == JoinRequest.Status.PENDING:
        join_request.status = JoinRequest.Status.CANCELLED
        join_request.responded_at = timezone.now()
        join_request.save(update_fields=['status', 'responded_at'])
        messages.info(request, _('Request cancelled.'))
    return redirect('teams:home')


# --- Smart recommendations ------------------------------------------------
@login_required
@participant_required
def team_recommendations(request):
    """Recommend the teams in a chosen hackathon that best fit the participant.

    Candidate set mirrors the existing "discover" visibility rule for
    participants: only *public*, *active* teams of the selected hackathon are
    considered, so no private team data is ever surfaced. Teams the participant
    already belongs to are excluded. Scoring is delegated to
    :mod:`teams.matching` (``rank_teams`` loads all memberships in a single
    query); eligibility to actually join is delegated to the authoritative
    ``Team.is_joinable(user)`` — this view creates no membership and never
    bypasses any permission.

    A team reaches the page only when all three server-side conditions hold:
    the team declared enough data for its score to mean something, the weighted
    score is at least ``matching.MIN_MATCH_SCORE``, and ``is_joinable()`` says
    the participant may request to join it (so full, archived, private,
    request-less or already-taken teams are never presented as suggestions).
    Candidates that were scored but filtered out are counted, not shown, so the
    template can state honestly that nothing good enough exists instead of
    padding the grid.

    Cost is bounded and independent of how many teams the hackathon holds: the
    membership counts, the participant's own memberships, the pending join
    requests and the pending invitations are each fetched in one query for the
    whole candidate set and fed to ``is_joinable()`` as pre-computed hints.

    The hackathon is selected through the ``?hackathon=<id>`` query param, the
    same convention used by the team list filter, limited to hackathons that are
    currently open for participants.
    """
    user = request.user
    # Same "open for participants" rule the team form applies: published and
    # the registration deadline not passed (a NULL deadline opens no window).
    selectable_hackathons = Hackathon.objects.filter(
        status=Hackathon.Status.PUBLISHED,
        registration_deadline__gte=timezone.now(),
    ).order_by('title')

    hackathon_id = request.GET.get('hackathon', '')
    selected = None
    if hackathon_id.isdigit():
        selected = selectable_hackathons.filter(pk=int(hackathon_id)).first()

    profile_has_data = bool(user.skills.strip() or user.interests.strip())

    results = []
    no_teams = False
    all_joined = False
    below_threshold = False
    no_open_slot = False
    if selected is not None:
        member_team_ids = set(
            TeamMembership.objects.filter(user=user).values_list('team_id', flat=True)
        )
        base = Team.objects.filter(
            hackathon=selected,
            status=Team.Status.ACTIVE,
            visibility=Team.Visibility.PUBLIC,
        )
        teams_list = list(
            base.exclude(pk__in=member_team_ids)
            .select_related('hackathon', 'captain')
        )
        total_public = base.count()

        if not teams_list:
            no_teams = total_public == 0
            all_joined = total_public > 0
        else:
            candidate_ids = [t.pk for t in teams_list]
            # One query each for the whole candidate set (no per-team cost).
            member_counts = dict(
                TeamMembership.objects.filter(team_id__in=candidate_ids)
                .values_list('team_id').annotate(c=Count('id'))
            )
            # "One team per hackathon": the teams of *this* hackathon the
            # participant already belongs to (they were dropped from the
            # candidates, so any hit is necessarily a *different* team).
            hack_team_ids = set(
                TeamMembership.objects.filter(
                    user=user, team__hackathon=selected,
                ).values_list('team_id', flat=True)
            )
            pending_request_ids = set(
                JoinRequest.objects.filter(
                    user=user, status=JoinRequest.Status.PENDING,
                    team_id__in=candidate_ids,
                ).values_list('team_id', flat=True)
            )
            pending_invitation_ids = set(
                TeamInvitation.objects.filter(
                    invitee=user, status=TeamInvitation.Status.PENDING,
                    team_id__in=candidate_ids,
                ).values_list('team_id', flat=True)
            )

            team_by_id = {t.pk: t for t in teams_list}
            scored = 0
            above_threshold = 0
            for res in matching.rank_teams(user, teams_list):
                team = team_by_id[res.team_id]
                scored += 1
                if not matching.is_recommendable(res):
                    continue
                above_threshold += 1
                count = member_counts.get(team.pk, 0)
                joinable, join_reason = team.is_joinable(
                    user,
                    member_count=count,
                    user_is_member=False,  # members are excluded from candidates
                    user_has_team_in_hackathon=bool(hack_team_ids - {team.pk}),
                )
                if not joinable:
                    continue
                results.append({
                    'team': team,
                    'match': res.to_context(),
                    'member_count': count,
                    'remaining_slots': max(team.max_members - count, 0),
                    'joinable': joinable,
                    'join_reason': join_reason,
                    'has_pending_request': team.pk in pending_request_ids,
                    'has_invitation': team.pk in pending_invitation_ids,
                })
            # Two different honest states: nothing fitted the profile, or
            # something fitted but no seat is open. Saying "no compatible team"
            # in the second case would be a lie.
            below_threshold = scored > 0 and above_threshold == 0
            no_open_slot = above_threshold > 0 and not results

    context = {
        'hackathons': selectable_hackathons,
        'selected_hackathon': selected,
        'sel_hackathon': hackathon_id,
        'results': results,
        'result_count': len(results),
        'profile_has_data': profile_has_data,
        'no_teams': no_teams,
        'all_joined': all_joined,
        'below_threshold': below_threshold,
        'no_open_slot': no_open_slot,
        'min_score': matching.MIN_MATCH_SCORE,
        'weights': {
            'skills': int(matching.SKILLS_WEIGHT * 100),
            'interests': int(matching.INTERESTS_WEIGHT * 100),
            'complementarity': int(matching.COMPLEMENTARITY_WEIGHT * 100),
        },
    }
    return render(request, 'teams/team_recommendations.html', context)
