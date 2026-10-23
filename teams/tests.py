from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from hackathons.models import Hackathon
from teams import matching
from teams.forms import TeamForm
from teams.models import JoinRequest, Team, TeamInvitation, TeamMembership

User = get_user_model()
P = User.Role.PARTICIPANT
O = User.Role.ORGANIZER
J = User.Role.JURY


def make_user(username, role=P, **kw):
    return User.objects.create_user(username=username, email=f'{username}@ex.com', password='pass12345', role=role, **kw)


class TeamsBase(TestCase):
    def setUp(self):
        self.org = make_user('org1', O)
        self.other_org = make_user('org2', O)
        self.captain = make_user('cap', P, first_name='Cap', last_name='Tain')
        self.member = make_user('mem', P)
        self.outsider = make_user('out', P)
        self.jury = make_user('jury', J)

        self.hack = Hackathon.objects.create(
            name='AI Challenge', status=Hackathon.Status.REGISTRATION_OPEN,
            max_team_size=3, allow_multiple_teams=False, created_by=self.org,
        )
        self.other_hack = Hackathon.objects.create(
            name='Web Weekend', status=Hackathon.Status.REGISTRATION_OPEN,
            created_by=self.other_org,
        )
        self.team = Team.objects.create(
            name='Code Warriors', hackathon=self.hack, created_by=self.captain,
            captain=self.captain, max_members=3,
        )
        self.team.add_member(self.captain, role=TeamMembership.Role.CAPTAIN)
        self.team.add_member(self.member)

    def login(self, user):
        self.client.force_login(user)


class AuthGatingTests(TeamsBase):
    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(reverse('teams:home'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.url)

    def test_list_page_renders_for_participant(self):
        self.login(self.captain)
        resp = self.client.get(reverse('teams:home'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Code Warriors')

    def test_detail_page_renders(self):
        self.login(self.captain)
        resp = self.client.get(reverse('teams:detail', args=[self.team.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Code Warriors')


class TeamCreateTests(TeamsBase):
    def test_create_makes_creator_captain(self):
        self.login(self.outsider)
        url = reverse('teams:create')
        resp = self.client.post(url, {
            'name': 'New Squad', 'description': 'hi', 'hackathon': self.hack.pk,
            'max_members': 4, 'visibility': 'public', 'allow_join_requests': 'on',
        })
        self.assertEqual(resp.status_code, 302)
        team = Team.objects.get(name='New Squad')
        self.assertEqual(team.created_by, self.outsider)
        self.assertEqual(team.captain, self.outsider)
        m = TeamMembership.objects.get(team=team, user=self.outsider)
        self.assertEqual(m.role, TeamMembership.Role.CAPTAIN)

    def test_form_validation_requires_name(self):
        self.login(self.outsider)
        resp = self.client.post(reverse('teams:create'), {'name': '', 'max_members': 4, 'visibility': 'public'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('name', resp.context['form'].errors)

    def test_participant_only_can_create(self):
        self.login(self.jury)
        resp = self.client.post(reverse('teams:create'), {'name': 'JuryTeam', 'max_members': 4, 'visibility': 'public'})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Team.objects.filter(name='JuryTeam').exists())


class EditPermissionTests(TeamsBase):
    def test_captain_can_edit(self):
        self.login(self.captain)
        resp = self.client.post(reverse('teams:edit', args=[self.team.pk]), {
            'name': 'Code Warriors', 'description': 'updated', 'hackathon': self.hack.pk,
            'max_members': 3, 'visibility': 'public', 'allow_join_requests': 'on',
        })
        self.assertEqual(resp.status_code, 302)
        self.team.refresh_from_db()
        self.assertEqual(self.team.description, 'updated')

    def test_non_captain_cannot_edit(self):
        self.login(self.member)
        resp = self.client.post(reverse('teams:edit', args=[self.team.pk]), {
            'name': 'Hijacked', 'max_members': 3, 'visibility': 'public',
        })
        self.assertEqual(resp.status_code, 302)
        self.team.refresh_from_db()
        self.assertNotEqual(self.team.name, 'Hijacked')

    def test_jury_cannot_edit(self):
        self.login(self.jury)
        resp = self.client.post(reverse('teams:edit', args=[self.team.pk]), {
            'name': 'JuryEdit', 'max_members': 3, 'visibility': 'public',
        })
        self.assertEqual(resp.status_code, 302)
        self.team.refresh_from_db()
        self.assertNotEqual(self.team.name, 'JuryEdit')


class CapacityAndMembershipTests(TeamsBase):
    def test_duplicate_membership_blocked(self):
        self.team.add_member(self.member)
        self.assertEqual(TeamMembership.objects.filter(team=self.team, user=self.member).count(), 1)

    def test_join_respects_capacity(self):
        for _ in range(1):
            pass
        u = make_user('filler', P)
        ok, _ = self.team.is_joinable(u)
        self.assertTrue(ok)
        # fill to capacity
        self.team.add_member(u)
        self.assertTrue(self.team.is_full())
        self.assertFalse(self.team.is_joinable(make_user('late', P))[0])

    def test_cannot_join_two_teams_same_hackathon(self):
        team2 = Team.objects.create(name='Second', hackathon=self.hack, created_by=self.captain,
                                    captain=self.captain, max_members=5)
        self.login(self.member)  # member is already in self.team (same hackathon)
        resp = self.client.post(reverse('teams:request_join', args=[team2.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(team2.has_member(self.member))
        self.assertFalse(JoinRequest.objects.filter(team=team2, user=self.member).exists())

    def test_join_public_team_via_view(self):
        team2 = Team.objects.create(name='Open', hackathon=self.hack, created_by=self.captain,
                                    captain=self.captain, max_members=5)
        self.login(self.outsider)
        resp = self.client.post(reverse('teams:request_join', args=[team2.pk]))
        self.assertEqual(resp.status_code, 302)
        # No direct membership: only a pending request is created.
        self.assertFalse(team2.has_member(self.outsider))
        jr = JoinRequest.objects.get(team=team2, user=self.outsider)
        self.assertEqual(jr.status, JoinRequest.Status.PENDING)
        # Captain approves to complete the join.
        self.login(self.captain)
        self.client.post(reverse('teams:join_request_respond', args=[jr.pk]), {'action': 'accept'})
        self.assertTrue(team2.has_member(self.outsider))

    def test_leave_removes_member(self):
        self.login(self.member)
        resp = self.client.post(reverse('teams:leave', args=[self.team.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(self.team.has_member(self.member))

    def test_captain_cannot_leave_while_members_remain(self):
        self.login(self.captain)
        resp = self.client.post(reverse('teams:leave', args=[self.team.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self.team.has_member(self.captain))


class CaptaincyTests(TeamsBase):
    def test_transfer_captaincy(self):
        self.login(self.captain)
        resp = self.client.post(reverse('teams:transfer_captain', args=[self.team.pk]),
                                {'new_captain': self.member.pk})
        self.assertEqual(resp.status_code, 302)
        self.team.refresh_from_db()
        self.assertEqual(self.team.captain, self.member)
        self.assertEqual(self.team.membership_for(self.member).role, TeamMembership.Role.CAPTAIN)
        self.assertEqual(self.team.membership_for(self.captain).role, TeamMembership.Role.MEMBER)

    def test_remove_member_by_captain(self):
        outsider = make_user('temp', P)
        self.team.add_member(outsider)
        self.login(self.captain)
        ms = self.team.membership_for(outsider)
        resp = self.client.post(reverse('teams:remove_member', args=[self.team.pk, ms.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(self.team.has_member(outsider))

    def test_cannot_remove_captain(self):
        self.login(self.captain)
        ms = self.team.membership_for(self.captain)
        resp = self.client.post(reverse('teams:remove_member', args=[self.team.pk, ms.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self.team.has_member(self.captain))

    def test_non_captain_cannot_remove(self):
        self.login(self.member)
        ms = self.team.membership_for(self.captain)
        resp = self.client.post(reverse('teams:remove_member', args=[self.team.pk, ms.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self.team.has_member(self.captain))


class InvitationTests(TeamsBase):
    def test_captain_sends_invitation(self):
        self.login(self.captain)
        resp = self.client.post(reverse('teams:send_invitation', args=[self.team.pk]),
                                {'invitee': self.outsider.pk, 'message': 'join us'})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(TeamInvitation.objects.filter(team=self.team, invitee=self.outsider).exists())

    def test_accept_invitation_adds_member(self):
        inv = TeamInvitation.objects.create(
            team=self.team, inviter=self.captain, invitee=self.outsider,
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.login(self.outsider)
        resp = self.client.post(reverse('teams:invitation_accept', args=[inv.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self.team.has_member(self.outsider))
        inv.refresh_from_db()
        self.assertEqual(inv.status, TeamInvitation.Status.ACCEPTED)

    def test_decline_invitation(self):
        inv = TeamInvitation.objects.create(team=self.team, inviter=self.captain, invitee=self.outsider)
        self.login(self.outsider)
        self.client.post(reverse('teams:invitation_decline', args=[inv.pk]))
        inv.refresh_from_db()
        self.assertEqual(inv.status, TeamInvitation.Status.DECLINED)
        self.assertFalse(self.team.has_member(self.outsider))

    def test_cannot_accept_another_users_invitation(self):
        inv = TeamInvitation.objects.create(team=self.team, inviter=self.captain, invitee=self.outsider)
        self.login(self.member)
        self.client.post(reverse('teams:invitation_accept', args=[inv.pk]))
        inv.refresh_from_db()
        self.assertEqual(inv.status, TeamInvitation.Status.PENDING)
        # The intended invitee must not have been added by another user's action.
        self.assertFalse(self.team.has_member(self.outsider))

    def test_cannot_accept_expired_invitation(self):
        inv = TeamInvitation.objects.create(
            team=self.team, inviter=self.captain, invitee=self.outsider,
            expires_at=timezone.now() - timedelta(days=1),
        )
        self.login(self.outsider)
        self.client.post(reverse('teams:invitation_accept', args=[inv.pk]))
        self.assertFalse(self.team.has_member(self.outsider))

    def test_invitation_cannot_exceed_capacity(self):
        Team.objects  # noqa
        filler = make_user('fill', P)
        self.team.add_member(filler)  # now 3/3
        self.assertTrue(self.team.is_full())
        inv = TeamInvitation.objects.create(
            team=self.team, inviter=self.captain, invitee=self.outsider,
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.login(self.outsider)
        self.client.post(reverse('teams:invitation_accept', args=[inv.pk]))
        self.assertFalse(self.team.has_member(self.outsider))

    def test_cancel_invitation_by_sender(self):
        inv = TeamInvitation.objects.create(team=self.team, inviter=self.captain, invitee=self.outsider)
        self.login(self.captain)
        self.client.post(reverse('teams:invitation_cancel', args=[inv.pk]))
        inv.refresh_from_db()
        self.assertEqual(inv.status, TeamInvitation.Status.CANCELLED)


class JoinRequestTests(TeamsBase):
    def test_request_and_approve(self):
        self.login(self.outsider)
        self.client.post(reverse('teams:request_join', args=[self.team.pk]))
        jr = JoinRequest.objects.get(team=self.team, user=self.outsider)
        self.assertEqual(jr.status, JoinRequest.Status.PENDING)

        self.login(self.captain)
        self.client.post(reverse('teams:join_request_respond', args=[jr.pk]), {'action': 'accept'})
        jr.refresh_from_db()
        self.assertEqual(jr.status, JoinRequest.Status.ACCEPTED)
        self.assertTrue(self.team.has_member(self.outsider))

    def test_reject_request(self):
        self.login(self.outsider)
        self.client.post(reverse('teams:request_join', args=[self.team.pk]))
        jr = JoinRequest.objects.get(team=self.team, user=self.outsider)
        self.login(self.captain)
        self.client.post(reverse('teams:join_request_respond', args=[jr.pk]), {'action': 'reject'})
        jr.refresh_from_db()
        self.assertEqual(jr.status, JoinRequest.Status.REJECTED)
        self.assertFalse(self.team.has_member(self.outsider))

    def test_join_button_creates_request_not_membership(self):
        # The list/detail "Join" action routes to request_join: a participant
        # clicking it must NOT be added directly, only create a pending request.
        self.login(self.outsider)
        resp = self.client.post(reverse('teams:request_join', args=[self.team.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(self.team.has_member(self.outsider))
        self.assertEqual(
            JoinRequest.objects.filter(team=self.team, user=self.outsider,
                                       status=JoinRequest.Status.PENDING).count(), 1,
        )

    def test_duplicate_requests_blocked(self):
        self.login(self.outsider)
        self.client.post(reverse('teams:request_join', args=[self.team.pk]))
        self.client.post(reverse('teams:request_join', args=[self.team.pk]))
        self.assertEqual(
            JoinRequest.objects.filter(team=self.team, user=self.outsider,
                                       status=JoinRequest.Status.PENDING).count(), 1,
        )

    def test_member_cannot_request(self):
        self.login(self.member)
        self.client.post(reverse('teams:request_join', args=[self.team.pk]))
        self.assertFalse(JoinRequest.objects.filter(user=self.member).exists())

    def test_accept_blocked_when_full(self):
        self.login(self.outsider)
        self.client.post(reverse('teams:request_join', args=[self.team.pk]))
        jr = JoinRequest.objects.get(team=self.team, user=self.outsider)
        # Fill to capacity (max_members=3; captain + member already there).
        self.team.add_member(make_user('filler2', P))
        self.assertTrue(self.team.is_full())
        self.login(self.captain)
        resp = self.client.post(reverse('teams:join_request_respond', args=[jr.pk]), {'action': 'accept'})
        self.assertEqual(resp.status_code, 302)
        jr.refresh_from_db()
        self.assertEqual(jr.status, JoinRequest.Status.PENDING)  # left pending
        self.assertFalse(self.team.has_member(self.outsider))

    def test_accept_blocked_when_user_in_other_team_same_hackathon(self):
        # A pending request already exists, then the requester joins another
        # team in the same hackathon; approval must re-check and refuse.
        jr = JoinRequest.objects.create(team=self.team, user=self.outsider)
        other = Team.objects.create(name='Rivals', hackathon=self.hack, created_by=self.member,
                                    captain=self.member, max_members=5)
        other.add_member(self.member, role=TeamMembership.Role.CAPTAIN)
        other.add_member(self.outsider)
        self.login(self.captain)
        self.client.post(reverse('teams:join_request_respond', args=[jr.pk]), {'action': 'accept'})
        jr.refresh_from_db()
        self.assertEqual(jr.status, JoinRequest.Status.PENDING)
        self.assertFalse(self.team.has_member(self.outsider))

    def test_only_captain_can_respond(self):
        self.login(self.outsider)
        self.client.post(reverse('teams:request_join', args=[self.team.pk]))
        jr = JoinRequest.objects.get(team=self.team, user=self.outsider)
        self.login(self.member)  # member, not captain
        resp = self.client.post(reverse('teams:join_request_respond', args=[jr.pk]), {'action': 'accept'})
        self.assertEqual(resp.status_code, 302)
        jr.refresh_from_db()
        self.assertEqual(jr.status, JoinRequest.Status.PENDING)
        self.assertFalse(self.team.has_member(self.outsider))

    def test_requester_can_cancel(self):
        self.login(self.outsider)
        self.client.post(reverse('teams:request_join', args=[self.team.pk]))
        jr = JoinRequest.objects.get(team=self.team, user=self.outsider)
        self.client.post(reverse('teams:join_request_cancel', args=[jr.pk]))
        jr.refresh_from_db()
        self.assertEqual(jr.status, JoinRequest.Status.CANCELLED)

    def test_captain_list_shows_pending_count(self):
        self.login(self.outsider)
        self.client.post(reverse('teams:request_join', args=[self.team.pk]))
        self.login(self.captain)
        resp = self.client.get(reverse('teams:home'))
        self.assertEqual(resp.context['pending_join_requests'], 1)

    # --- Priority fixes: hardened eligibility on every join path ----------
    def test_direct_join_route_removed(self):
        from django.urls import NoReverseMatch
        with self.assertRaises(NoReverseMatch):
            reverse('teams:join', args=[self.team.pk])

    def test_request_blocked_on_private_team(self):
        private = Team.objects.create(name='Secret', hackathon=self.hack, created_by=self.captain,
                                      captain=self.captain, max_members=5, visibility='private')
        self.login(self.outsider)
        self.client.post(reverse('teams:request_join', args=[private.pk]))
        self.assertFalse(JoinRequest.objects.filter(team=private).exists())

    def test_request_blocked_when_join_requests_disabled(self):
        no_req = Team.objects.create(name='NoRequests', hackathon=self.hack, created_by=self.captain,
                                     captain=self.captain, max_members=5, allow_join_requests=False)
        self.login(self.outsider)
        self.client.post(reverse('teams:request_join', args=[no_req.pk]))
        self.assertFalse(JoinRequest.objects.filter(team=no_req).exists())

    def test_request_blocked_on_archived_team(self):
        archived = Team.objects.create(name='Archived', hackathon=self.hack, created_by=self.captain,
                                       captain=self.captain, max_members=5, status=Team.Status.ARCHIVED)
        self.login(self.outsider)
        self.client.post(reverse('teams:request_join', args=[archived.pk]))
        self.assertFalse(JoinRequest.objects.filter(team=archived).exists())

    def test_request_blocked_on_closed_hackathon(self):
        self.hack.status = Hackathon.Status.CLOSED
        self.hack.save(update_fields=['status'])
        self.login(self.outsider)
        self.client.post(reverse('teams:request_join', args=[self.team.pk]))
        self.assertFalse(JoinRequest.objects.filter(team=self.team).exists())

    def test_approve_revalidates_closed_hackathon(self):
        # Request created while open, then the hackathon closes before approval.
        self.login(self.outsider)
        self.client.post(reverse('teams:request_join', args=[self.team.pk]))
        jr = JoinRequest.objects.get(team=self.team, user=self.outsider)
        self.hack.status = Hackathon.Status.CLOSED
        self.hack.save(update_fields=['status'])
        self.login(self.captain)
        self.client.post(reverse('teams:join_request_respond', args=[jr.pk]), {'action': 'accept'})
        jr.refresh_from_db()
        self.assertEqual(jr.status, JoinRequest.Status.PENDING)
        self.assertFalse(self.team.has_member(self.outsider))

    def test_approve_revalidates_team_made_private(self):
        self.login(self.outsider)
        self.client.post(reverse('teams:request_join', args=[self.team.pk]))
        jr = JoinRequest.objects.get(team=self.team, user=self.outsider)
        self.team.visibility = Team.Visibility.PRIVATE
        self.team.save(update_fields=['visibility'])
        self.login(self.captain)
        self.client.post(reverse('teams:join_request_respond', args=[jr.pk]), {'action': 'accept'})
        jr.refresh_from_db()
        self.assertEqual(jr.status, JoinRequest.Status.PENDING)
        self.assertFalse(self.team.has_member(self.outsider))

    def test_approve_revalidates_join_requests_disabled(self):
        self.login(self.outsider)
        self.client.post(reverse('teams:request_join', args=[self.team.pk]))
        jr = JoinRequest.objects.get(team=self.team, user=self.outsider)
        self.team.allow_join_requests = False
        self.team.save(update_fields=['allow_join_requests'])
        self.login(self.captain)
        self.client.post(reverse('teams:join_request_respond', args=[jr.pk]), {'action': 'accept'})
        jr.refresh_from_db()
        self.assertEqual(jr.status, JoinRequest.Status.PENDING)
        self.assertFalse(self.team.has_member(self.outsider))




class RoleScopingTests(TeamsBase):
    def test_organizer_only_sees_own_hackathon_teams(self):
        self.login(self.org)
        resp = self.client.get(reverse('teams:home'))
        names = [t.name for t in resp.context['browse']]
        self.assertIn('Code Warriors', names)

        self.login(self.other_org)
        resp = self.client.get(reverse('teams:home'))
        names = [t.name for t in resp.context['browse']]
        self.assertNotIn('Code Warriors', names)

    def test_jury_cannot_join_or_invite(self):
        team2 = Team.objects.create(name='JuryTarget', hackathon=self.hack, created_by=self.captain,
                                    captain=self.captain, max_members=5, visibility='public')
        self.login(self.jury)
        self.client.post(reverse('teams:request_join', args=[team2.pk]))
        self.assertFalse(team2.has_member(self.jury))
        self.assertFalse(JoinRequest.objects.filter(user=self.jury).exists())
        self.client.post(reverse('teams:send_invitation', args=[self.team.pk]), {'invitee': self.outsider.pk})
        self.assertFalse(TeamInvitation.objects.filter(inviter=self.jury).exists())

    def test_filter_by_search(self):
        self.login(self.captain)
        resp = self.client.get(reverse('teams:home') + '?q=Warriors')
        self.assertContains(resp, 'Code Warriors')
        resp = self.client.get(reverse('teams:home') + '?q=nomatch')
        self.assertNotContains(resp, 'Code Warriors')

    def test_disband_by_captain(self):
        self.login(self.captain)
        resp = self.client.post(reverse('teams:disband', args=[self.team.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Team.objects.filter(pk=self.team.pk).exists())

    def test_non_captain_cannot_disband(self):
        self.login(self.member)
        self.client.post(reverse('teams:disband', args=[self.team.pk]))
        self.assertTrue(Team.objects.filter(pk=self.team.pk).exists())


class SelectForUpdateAutocommitTests(TransactionTestCase):
    """select_for_update() must not run outside a transaction.

    These views use row locks; TestCase silently passes them because every test
    is wrapped in an outer atomic. TransactionTestCase runs in autocommit like
    the real server, so it catches the regression if the atomic wrapper is
    removed from _respond_invitation / _respond_join_request.
    """

    def setUp(self):
        self.captain = make_user('t_cap', P)
        self.invitee = make_user('t_invitee', P)
        self.requester = make_user('t_requester', P)
        self.hack = Hackathon.objects.create(
            name='Lock Test', status=Hackathon.Status.REGISTRATION_OPEN,
            max_team_size=5, created_by=self.captain,
        )
        self.team = Team.objects.create(
            name='Lock Team', hackathon=self.hack, created_by=self.captain,
            captain=self.captain, max_members=5, allow_join_requests=True,
        )
        self.team.add_member(self.captain, role=TeamMembership.Role.CAPTAIN)

    def test_accept_invitation_in_autocommit(self):
        inv = TeamInvitation.objects.create(
            team=self.team, inviter=self.captain, invitee=self.invitee,
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.client.force_login(self.invitee)
        resp = self.client.post(reverse('teams:invitation_accept', args=[inv.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self.team.has_member(self.invitee))

    def test_decline_invitation_in_autocommit(self):
        inv = TeamInvitation.objects.create(team=self.team, inviter=self.captain, invitee=self.invitee)
        self.client.force_login(self.invitee)
        resp = self.client.post(reverse('teams:invitation_decline', args=[inv.pk]))
        self.assertEqual(resp.status_code, 302)

    def test_respond_join_request_in_autocommit(self):
        req = JoinRequest.objects.create(team=self.team, user=self.requester, message='me')
        self.client.force_login(self.captain)
        resp = self.client.post(
            reverse('teams:join_request_respond', args=[req.pk]), {'action': 'accept'},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self.team.has_member(self.requester))


class TeamSkillsFieldsTests(TeamsBase):
    def test_new_team_fields_default_empty(self):
        team = Team.objects.create(name='Blank', hackathon=self.hack,
                                   created_by=self.captain, captain=self.captain)
        self.assertEqual(team.required_skills, '')
        self.assertEqual(team.interests, '')

    def test_create_view_persists_skills_and_interests(self):
        self.login(self.outsider)
        resp = self.client.post(reverse('teams:create'), {
            'name': 'Skill Squad', 'hackathon': self.hack.pk, 'max_members': 4,
            'visibility': 'public', 'allow_join_requests': 'on',
            'required_skills': 'Backend, DevOps', 'interests': 'Fintech',
        })
        self.assertEqual(resp.status_code, 302)
        team = Team.objects.get(name='Skill Squad')
        self.assertEqual(team.required_skills, 'Backend, DevOps')
        self.assertEqual(team.interests, 'Fintech')

    def test_create_view_accepts_empty_skills_and_interests(self):
        self.login(self.outsider)
        resp = self.client.post(reverse('teams:create'), {
            'name': 'NoSkills', 'hackathon': self.hack.pk, 'max_members': 4,
            'visibility': 'public', 'allow_join_requests': 'on',
            'required_skills': '', 'interests': '',
        })
        self.assertEqual(resp.status_code, 302)
        team = Team.objects.get(name='NoSkills')
        self.assertEqual(team.required_skills, '')
        self.assertEqual(team.interests, '')

    def test_edit_view_updates_and_replaces(self):
        self.login(self.captain)
        resp = self.client.post(reverse('teams:edit', args=[self.team.pk]), {
            'name': 'Code Warriors', 'hackathon': self.hack.pk, 'max_members': 3,
            'visibility': 'public', 'allow_join_requests': 'on',
            'required_skills': 'ML, Python', 'interests': 'Health',
        })
        self.assertEqual(resp.status_code, 302)
        self.team.refresh_from_db()
        self.assertEqual(self.team.required_skills, 'ML, Python')
        self.assertEqual(self.team.interests, 'Health')

    def test_edit_form_renders_current_values(self):
        self.team.required_skills = 'Design, UX'
        self.team.interests = 'Gaming'
        self.team.save()
        self.login(self.captain)
        resp = self.client.get(reverse('teams:edit', args=[self.team.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Design, UX')
        self.assertContains(resp, 'Gaming')

    def test_team_form_allows_blank_new_fields(self):
        form = TeamForm(data={
            'name': 'BlankForm', 'hackathon': self.hack.pk, 'max_members': 5,
            'visibility': 'public', 'required_skills': '', 'interests': '',
        })
        self.assertTrue(form.is_valid(), form.errors)


# --------------------------------------------------------------------------
# Smart matching engine (teams/matching.py)
# --------------------------------------------------------------------------
class NormalizeTests(SimpleTestCase):
    """Pure text normalisation — no database access."""

    def test_case_insensitive_and_trims_spaces(self):
        self.assertEqual(matching.normalize('  PYTHON , Django '), ['python', 'django'])

    def test_splits_on_commas_drops_empties(self):
        self.assertEqual(matching.normalize('a, ,b,'), ['a', 'b'])

    def test_deduplicates_preserving_order(self):
        self.assertEqual(matching.normalize('b, a, B, b'), ['b', 'a'])

    def test_empty_and_none(self):
        self.assertEqual(matching.normalize(''), [])
        self.assertEqual(matching.normalize(None), [])

    def test_does_not_collapse_synonyms(self):
        self.assertEqual(matching.normalize('JS, JavaScript'), ['js', 'javascript'])


class MatchingScoringTests(SimpleTestCase):
    """Component maths and explainability on in-memory instances (no DB)."""

    @staticmethod
    def _user(skills='', interests='', pk=1):
        return User(pk=pk, username='u', skills=skills, interests=interests)

    @staticmethod
    def _team(required='', interests='', name='T'):
        return Team(name=name, required_skills=required, interests=interests)

    def test_exact_match_full_score(self):
        result = matching.score_team(
            self._user('PYTHON,django', 'ai, WEB'),
            self._team('python , Django', 'web,ai'),
            member_skills=set(),
        )
        self.assertEqual(result.score, 100)
        self.assertEqual(result.skills_score, 100.0)
        self.assertEqual(result.interests_score, 100.0)
        self.assertEqual(result.complementarity_score, 100.0)
        self.assertEqual(result.matched_skills, ['python', 'django'])

    def test_no_overlap_scores_zero(self):
        result = matching.score_team(
            self._user('Go', 'Music'), self._team('Python', 'AI'), member_skills=set(),
        )
        self.assertEqual(result.score, 0)
        self.assertEqual(result.missing_skills, ['python'])

    def test_case_and_spacing_are_normalized(self):
        result = matching.score_team(
            self._user('  python ,Django '), self._team('Python, Django'), member_skills=set(),
        )
        self.assertEqual(result.skills_score, 100.0)
        self.assertEqual(result.matched_skills, ['python', 'django'])

    def test_duplicates_and_blanks_handled(self):
        result = matching.score_team(
            self._user('Python,, PYTHON , python '), self._team('python, python, , '),
            member_skills=set(),
        )
        self.assertEqual(result.matched_skills, ['python'])
        self.assertEqual(result.skills_score, 100.0)

    def test_team_without_required_skills(self):
        result = matching.score_team(
            self._user('Python', 'ai'), self._team('', 'ai'), member_skills=set(),
        )
        self.assertEqual(result.skills_score, 0.0)
        self.assertEqual(result.score, 30)  # only interests (30%) contribute
        self.assertIn(matching._SKILLS_MISSING, result.explanations)

    def test_team_without_interests(self):
        result = matching.score_team(
            self._user('python', 'x'), self._team('python', ''), member_skills=set(),
        )
        self.assertEqual(result.interests_score, 0.0)
        self.assertEqual(result.score, 70)  # skills 50 + complementarity 20
        self.assertIn(matching._INTERESTS_MISSING, result.explanations)

    def test_complementarity_when_partially_covered(self):
        result = matching.score_team(
            self._user('B'), self._team('A, B'), member_skills={'a'},
        )
        self.assertEqual(result.skills_score, 50.0)
        self.assertEqual(result.uncovered_team_skills, ['b'])
        self.assertEqual(result.complementarity_score, 100.0)
        self.assertEqual(result.score, 45)  # 0.5*50 + 0.2*100

    def test_complementarity_zero_when_all_covered(self):
        result = matching.score_team(
            self._user('C'), self._team('A, B'), member_skills={'a', 'b'},
        )
        self.assertEqual(result.uncovered_team_skills, [])
        self.assertEqual(result.complementarity_score, 0.0)
        self.assertIn(matching._ALL_SKILLS_COVERED, result.explanations)

    def test_weights_are_named_constants_summing_to_one(self):
        self.assertAlmostEqual(
            matching.SKILLS_WEIGHT + matching.INTERESTS_WEIGHT + matching.COMPLEMENTARITY_WEIGHT,
            1.0,
        )

    def test_contributions_are_weighted_not_normalized(self):
        result = matching.score_team(self._user('python'), self._team('python'), member_skills=set())
        # skills normalised 0..100 is 100, but its weighted contribution is 50.
        self.assertEqual(result.skills_score, 100.0)
        self.assertAlmostEqual(result.contributions['skills'], 50.0)

    def test_score_never_exceeds_bounds(self):
        result = matching.score_team(
            self._user('a'), self._team('a,b,c', 'x'), member_skills=set(),
        )
        self.assertEqual(result.score, 23)
        self.assertTrue(0 <= result.score <= 100)

    def test_insufficient_data_flag(self):
        result = matching.score_team(
            self._user('a', 'b'), self._team('', ''), member_skills=set(),
        )
        self.assertFalse(result.sufficient_data)
        self.assertEqual(result.score, 0)
        self.assertIn(matching._INSUFFICIENT_TEAM_DATA, result.explanations)


class MatchingDbSafetyTests(TeamsBase):
    """Engine must read only and never mutate stored rows."""

    def _participant(self, username, skills='', interests=''):
        return make_user(username, skills=skills, interests=interests)

    def _bare_team(self, name, **kw):
        return Team.objects.create(
            name=name, hackathon=self.hack, created_by=self.org,
            max_members=5, visibility='public', **kw,
        )

    def test_participant_skills_not_counted_as_current_coverage(self):
        team = self._bare_team('SelfRef', required_skills='b')
        p = self._participant('selfref_p', skills='b')
        team.add_member(p)  # participant is already a member
        self.assertTrue(team.has_member(p))
        result = matching.score_team(p, team)  # loads members from DB
        # The participant's own skill 'b' must NOT mark the requirement covered.
        self.assertIn('b', result.uncovered_team_skills)
        self.assertEqual(result.complementarity_score, 100.0)

    def test_rank_teams_orders_by_score_desc(self):
        weak = self._bare_team('Weak', required_skills='z', interests='q')
        strong = self._bare_team('Strong', required_skills='python, django', interests='ai')
        p = self._participant('rank_p', skills='Python, Django', interests='AI')
        ranked = matching.rank_teams(p, [weak, strong])
        self.assertEqual([r.team_id for r in ranked][0], strong.pk)
        self.assertGreaterEqual(ranked[0].score, ranked[-1].score)

    def test_rank_teams_loads_members_in_single_query(self):
        t1 = self._bare_team('RQ1', required_skills='python')
        t2 = self._bare_team('RQ2', required_skills='django')
        t3 = self._bare_team('RQ3', required_skills='go')
        p = self._participant('rq_p', skills='python')
        with CaptureQueriesContext(connection) as ctx:
            matching.rank_teams(p, [t1, t2, t3])
        membership_queries = [
            q for q in ctx.captured_queries
            if 'teams_teammembership' in q['sql'].lower()
        ]
        self.assertEqual(len(membership_queries), 1)

    def test_matching_does_not_modify_database(self):
        team = self._bare_team(
            'Immutable', required_skills='python, ml', interests='ai, web',
        )
        member = self._participant('im_member', skills='python')
        team.add_member(member)
        p = self._participant('im_p', skills='ml', interests='ai')

        team_snapshot = Team.objects.filter(pk=team.pk).values()
        counts_before = (
            Team.objects.count(), TeamMembership.objects.count(),
            JoinRequest.objects.count(), TeamInvitation.objects.count(),
        )

        matching.score_team(p, team)
        matching.rank_teams(p, [team])

        self.assertEqual(
            (Team.objects.count(), TeamMembership.objects.count(),
             JoinRequest.objects.count(), TeamInvitation.objects.count()),
            counts_before,
        )
        self.assertEqual(list(Team.objects.filter(pk=team.pk).values()), list(team_snapshot))
        team.refresh_from_db()
        self.assertEqual(team.required_skills, 'python, ml')
        self.assertEqual(team.interests, 'ai, web')
        member.refresh_from_db()
        self.assertEqual(member.skills, 'python')


class RecommendationsViewTests(TeamsBase):
    """Integrates the matching engine into the participant recommendations page."""

    def url(self, hackathon):
        return reverse('teams:recommendations') + f'?hackathon={hackathon.pk}'

    def _participant(self, name, skills='', interests=''):
        return make_user(name, skills=skills, interests=interests)

    def _team(self, name, **kw):
        defaults = dict(hackathon=self.hack, created_by=self.captain,
                        captain=self.captain, max_members=5,
                        visibility='public', status='active',
                        required_skills='', interests='')
        defaults.update(kw)
        return Team.objects.create(name=name, **defaults)

    def _item(self, results, team):
        return next(r for r in results if r['team'].pk == team.pk)

    # 1. Access
    def test_participant_can_access(self):
        self.login(self.outsider)
        resp = self.client.get(self.url(self.hack))
        self.assertEqual(resp.status_code, 200)

    def test_jury_redirected(self):
        self.login(self.jury)
        resp = self.client.get(self.url(self.hack))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/teams', resp['Location'])

    def test_organizer_redirected(self):
        self.login(self.org)
        resp = self.client.get(self.url(self.hack))
        self.assertEqual(resp.status_code, 302)

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(self.url(self.hack))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp['Location'])

    # 3. Hackathon filtering
    def test_only_relevant_hackathon_teams(self):
        self._team('OtherEvent', hackathon=self.other_hack, required_skills='python')
        self.login(self._participant('f1', skills='python'))
        resp = self.client.get(self.url(self.hack))
        self.assertNotContains(resp, 'OtherEvent')

    # 4. Exclude teams already joined
    def test_excludes_teams_user_is_member_of(self):
        self.login(self.member)  # member of self.team 'Code Warriors'
        resp = self.client.get(self.url(self.hack))
        self.assertNotContains(resp, 'Code Warriors')

    # 5. Ordering by score desc
    def test_results_ordered_by_score_desc(self):
        strong = self._team('Strong', required_skills='python, django', interests='ai, web')
        weak = self._team('Weak', required_skills='rust', interests='iot')
        self.login(self._participant('o2', skills='python, django', interests='ai, web'))
        results = self.client.get(self.url(self.hack)).context['results']
        names = [r['team'].name for r in results]
        self.assertEqual(names[0], 'Strong')
        scores = [r['match']['score'] for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertGreater(self._item(results, strong)['match']['score'],
                           self._item(results, weak)['match']['score'])

    # 6. Compatibility vs joinability
    def test_full_team_is_compatible_but_not_joinable(self):
        full = self._team('FullRoom', required_skills='python', max_members=1)
        full.add_member(self.captain)
        self.login(self._participant('o3', skills='python'))
        results = self.client.get(self.url(self.hack)).context['results']
        item = self._item(results, full)
        self.assertEqual(item['match']['skills_score'], 100.0)  # compatible
        self.assertFalse(item['joinable'])                       # but cannot join
        self.assertTrue(item['join_reason'])

    # 7. Respect archived / private / closed
    def test_archived_and_private_teams_excluded(self):
        self._team('ArchivedTeam', required_skills='python', status='archived')
        self._team('SecretTeam', required_skills='python', visibility='private')
        self.login(self._participant('o4', skills='python'))
        resp = self.client.get(self.url(self.hack))
        self.assertNotContains(resp, 'ArchivedTeam')
        self.assertNotContains(resp, 'SecretTeam')

    def test_closed_hackathon_is_not_selectable(self):
        self.hack.status = Hackathon.Status.CLOSED
        self.hack.save(update_fields=['status'])
        self.login(self._participant('o5', skills='python'))
        resp = self.client.get(self.url(self.hack))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.context['selected_hackathon'])
        self.assertEqual(resp.context['results'], [])

    # 8. Existing pending request / invitation
    def test_pending_request_surfaces_instead_of_join_action(self):
        team = self._team('PendingTeam', required_skills='python')
        p = self._participant('o6', skills='python')
        JoinRequest.objects.create(team=team, user=p)
        self.login(p)
        resp = self.client.get(self.url(self.hack))
        item = self._item(resp.context['results'], team)
        self.assertTrue(item['has_pending_request'])
        self.assertContains(resp, 'Request pending')

    def test_pending_invitation_surfaces(self):
        team = self._team('InviteTeam', required_skills='python')
        p = self._participant('o7', skills='python')
        TeamInvitation.objects.create(team=team, inviter=self.captain, invitee=p,
                                      expires_at=timezone.now() + timedelta(days=7))
        self.login(p)
        results = self.client.get(self.url(self.hack)).context['results']
        self.assertTrue(self._item(results, team)['has_invitation'])

    # 9. Empty profile
    def test_empty_profile_flag_and_no_crash(self):
        self.login(self._participant('blankprofile'))
        resp = self.client.get(self.url(self.hack))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context['profile_has_data'])

    # 10. Hackathon without recommended teams
    def test_hackathon_without_teams(self):
        empty = Hackathon.objects.create(name='EmptyHack', status=Hackathon.Status.REGISTRATION_OPEN,
                                         created_by=self.org)
        self.login(self._participant('o8', skills='python'))
        resp = self.client.get(self.url(empty))
        self.assertEqual(resp.context['results'], [])
        self.assertTrue(resp.context['no_teams'])

    # 11. No private data leak
    def test_does_not_leak_private_team_or_member_skills(self):
        priv = self._team('HiddenTeam', visibility='private', required_skills='python',
                          description='SECRETDESC')
        leak = self._team('LeakCheck', required_skills='python')
        other = self._participant('memx', skills='secretskill')
        leak.add_member(other)
        self.login(self._participant('o9', skills='python'))
        resp = self.client.get(self.url(self.hack))
        self.assertNotContains(resp, 'HiddenTeam')
        self.assertNotContains(resp, 'SECRETDESC')
        self.assertNotContains(resp, 'secretskill')

    # 12. Engine components used verbatim
    def test_components_and_breakdown_exposed(self):
        team = self._team('Alpha', required_skills='python', interests='ai')
        p = self._participant('o10', skills='python', interests='ai')
        self.login(p)
        results = self.client.get(self.url(self.hack)).context['results']
        match = self._item(results, team)['match']
        self.assertEqual(match['skills_score'], 100)
        self.assertEqual(match['interests_score'], 100)
        self.assertEqual(match['matched_skills'], ['python'])
        self.assertEqual(match['uncovered_team_skills'], ['python'])
        self.assertTrue(match['sufficient_data'])
        self.assertEqual(match['score'], 100)
