from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from hackathons.models import Hackathon
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
        self.login(self.member)
        resp = self.client.post(reverse('teams:join', args=[team2.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(team2.has_member(self.member))

    def test_join_public_team_via_view(self):
        team2 = Team.objects.create(name='Open', hackathon=self.hack, created_by=self.captain,
                                    captain=self.captain, max_members=5)
        self.login(self.outsider)
        resp = self.client.post(reverse('teams:join', args=[team2.pk]))
        self.assertEqual(resp.status_code, 302)
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
        self.client.post(reverse('teams:join', args=[team2.pk]))
        self.assertFalse(team2.has_member(self.jury))
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
