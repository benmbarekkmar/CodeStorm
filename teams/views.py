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
    if team.is_full():
        invitation.status = TeamInvitation.Status.EXPIRED
        invitation.responded_at = timezone.now()
        invitation.save(update_fields=['status', 'responded_at'])
        return False, _('This team is already full.')
    if team.has_member(user):
        invitation.status = TeamInvitation.Status.ACCEPTED
        invitation.responded_at = timezone.now()
        invitation.save(update_fields=['status', 'responded_at'])
        return False, _('You are already a member of this team.')
    if team.hackathon and not team.hackathon.allow_multiple_teams:
        if Team.objects.filter(hackathon=team.hackathon, memberships__user=user).exists():
            return False, _('You already have a team in this hackathon.')
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
            hackathon__created_by=user,
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
    pending_join_requests = sum(captain_pending.values())

    context = {
        'my_teams': my_teams,
        'discover': discover,
        'browse': browse,
        'stats': stats,
        'pending_invites': pending_invites,
        'pending_join_requests': pending_join_requests,
        'hackathons': Hackathon.objects.all(),
        'create_form': TeamForm() if user.role == PARTICIPANT else None,
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
