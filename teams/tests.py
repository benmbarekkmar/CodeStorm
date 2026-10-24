
import io
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from hackathons.models import Hackathon
from teams import matching
from teams.forms import InviteMemberForm, TeamForm
from teams.models import JoinRequest, Team, TeamInvitation, TeamMembership

User = get_user_model()
P = User.Role.PARTICIPANT
O = User.Role.ORGANIZER
J = User.Role.JURY


def make_user(username, role=P, **kw):
    return User.objects.create_user(
        username=username,
        email=f"{username}@ex.com",
        password="pass12345",
        role=role,
        **kw,
    )


def make_hackathon(organizer, title="Test Hackathon", max_team_size=5):
    now = timezone.now()
    return Hackathon.objects.create(
        organizer=organizer,
        title=title,
        description="Test hackathon",
        category=Hackathon.Category.AI,
        event_format=Hackathon.Format.ONLINE,
        start_date=now + timedelta(days=2),
        end_date=now + timedelta(days=5),
        registration_deadline=now + timedelta(days=1),
        submission_deadline=now + timedelta(days=4),
        min_team_size=2,
        max_team_size=max_team_size,
        status=Hackathon.Status.PUBLISHED,
    )


def add_membership(team, user, role=TeamMembership.Role.MEMBER):
    """Create a membership only if it does not already exist."""
    membership, _ = TeamMembership.objects.get_or_create(
        team=team,
        user=user,
        defaults={"role": role},
    )
    return membership


class TeamsBase(TestCase):
    def setUp(self):
        self.org = make_user("org1", O)
        self.other_org = make_user("org2", O)
        self.captain = make_user(
            "cap", P, first_name="Cap", last_name="Tain"
        )
        self.member = make_user("mem", P)
        self.outsider = make_user("out", P)
        self.jury = make_user("jury", J)

        self.hack = make_hackathon(
            self.org,
            title="AI Challenge",
            max_team_size=3,
        )
        self.other_hack = make_hackathon(
            self.other_org,
            title="Web Weekend",
        )

        self.team = Team.objects.create(
            name="Code Warriors",
            hackathon=self.hack,
            created_by=self.captain,
            captain=self.captain,
            max_members=3,
        )

        add_membership(
            self.team, self.captain, TeamMembership.Role.CAPTAIN
        )
        add_membership(
            self.team, self.member, TeamMembership.Role.MEMBER
        )

    def login(self, user):
        self.client.force_login(user)


class AuthGatingTests(TeamsBase):
    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(reverse("teams:home"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.url)

    def test_list_page_renders_for_participant(self):
        self.login(self.captain)
        resp = self.client.get(reverse("teams:home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Code Warriors")

    def test_detail_page_renders(self):
        self.login(self.captain)
        resp = self.client.get(
            reverse("teams:detail", args=[self.team.pk])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Code Warriors")


class TeamCreateTests(TeamsBase):
    def test_create_makes_creator_captain(self):
        self.login(self.outsider)
        resp = self.client.post(
            reverse("teams:create"),
            {
                "name": "New Squad",
                "description": "hi",
                "hackathon": self.hack.pk,
                "max_members": 4,
                "visibility": "public",
                "allow_join_requests": "on",
            },
        )
        self.assertEqual(resp.status_code, 302)

        team = Team.objects.get(name="New Squad")
        self.assertEqual(team.created_by, self.outsider)
        self.assertEqual(team.captain, self.outsider)

        membership = TeamMembership.objects.get(
            team=team, user=self.outsider
        )
        self.assertEqual(membership.role, TeamMembership.Role.CAPTAIN)

    def test_form_validation_requires_name(self):
        self.login(self.outsider)
        resp = self.client.post(
            reverse("teams:create"),
            {
                "name": "",
                "max_members": 4,
                "visibility": "public",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("name", resp.context["form"].errors)

    def test_participant_only_can_create(self):
        self.login(self.jury)
        resp = self.client.post(
            reverse("teams:create"),
            {
                "name": "JuryTeam",
                "max_members": 4,
                "visibility": "public",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Team.objects.filter(name="JuryTeam").exists())


class EditPermissionTests(TeamsBase):
    def test_captain_can_edit(self):
        self.login(self.captain)
        resp = self.client.post(
            reverse("teams:edit", args=[self.team.pk]),
            {
                "name": "Code Warriors",
                "description": "updated",
                "hackathon": self.hack.pk,
                "max_members": 3,
                "visibility": "public",
                "allow_join_requests": "on",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.team.refresh_from_db()
        self.assertEqual(self.team.description, "updated")

    def test_non_captain_cannot_edit(self):
        self.login(self.member)
        self.client.post(
            reverse("teams:edit", args=[self.team.pk]),
            {
                "name": "Hijacked",
                "max_members": 3,
                "visibility": "public",
            },
        )
        self.team.refresh_from_db()
        self.assertNotEqual(self.team.name, "Hijacked")

    def test_jury_cannot_edit(self):
        self.login(self.jury)
        self.client.post(
            reverse("teams:edit", args=[self.team.pk]),
            {
                "name": "JuryEdit",
                "max_members": 3,
                "visibility": "public",
            },
        )
        self.team.refresh_from_db()
        self.assertNotEqual(self.team.name, "JuryEdit")


class CapacityAndMembershipTests(TeamsBase):
    def test_duplicate_membership_blocked(self):
        # The member was already added in TeamsBase.setUp().
        self.assertEqual(
            TeamMembership.objects.filter(
                team=self.team, user=self.member
            ).count(),
            1,
        )

    def test_join_respects_capacity(self):
        user = make_user("filler", P)
        can_join, _ = self.team.is_joinable(user)
        self.assertTrue(can_join)

        add_membership(self.team, user)
        self.assertTrue(self.team.is_full())

        late_user = make_user("late", P)
        self.assertFalse(self.team.is_joinable(late_user)[0])

    def test_cannot_join_two_teams_same_hackathon(self):
        team2 = Team.objects.create(
            name="Second",
            hackathon=self.hack,
            created_by=self.captain,
            captain=self.captain,
            max_members=5,
        )
        self.login(self.member)
        resp = self.client.post(
            reverse("teams:request_join", args=[team2.pk])
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(team2.has_member(self.member))
        self.assertFalse(
            JoinRequest.objects.filter(
                team=team2, user=self.member
            ).exists()
        )

    def test_join_public_team_via_view(self):
        team2 = Team.objects.create(
            name="Open",
            hackathon=self.hack,
            created_by=self.captain,
            captain=self.captain,
            max_members=5,
        )
        self.login(self.outsider)
        resp = self.client.post(
            reverse("teams:request_join", args=[team2.pk])
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(team2.has_member(self.outsider))

        join_request = JoinRequest.objects.get(
            team=team2, user=self.outsider
        )
        self.assertEqual(
            join_request.status, JoinRequest.Status.PENDING
        )

        self.login(self.captain)
        self.client.post(
            reverse(
                "teams:join_request_respond",
                args=[join_request.pk],
            ),
            {"action": "accept"},
        )
        self.assertTrue(team2.has_member(self.outsider))

    def test_leave_removes_member(self):
        self.login(self.member)
        resp = self.client.post(
            reverse("teams:leave", args=[self.team.pk])
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(self.team.has_member(self.member))

    def test_captain_cannot_leave_while_members_remain(self):
        self.login(self.captain)
        resp = self.client.post(
            reverse("teams:leave", args=[self.team.pk])
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self.team.has_member(self.captain))


class CaptaincyTests(TeamsBase):
    def test_transfer_captaincy(self):
        self.login(self.captain)
        resp = self.client.post(
            reverse("teams:transfer_captain", args=[self.team.pk]),
            {"new_captain": self.member.pk},
        )
        self.assertEqual(resp.status_code, 302)

        self.team.refresh_from_db()
        self.assertEqual(self.team.captain, self.member)
        self.assertEqual(
            self.team.membership_for(self.member).role,
            TeamMembership.Role.CAPTAIN,
        )
        self.assertEqual(
            self.team.membership_for(self.captain).role,
            TeamMembership.Role.MEMBER,
        )

    def test_remove_member_by_captain(self):
        outsider = make_user("temp", P)
        add_membership(self.team, outsider)
        self.login(self.captain)

        membership = self.team.membership_for(outsider)
        resp = self.client.post(
            reverse(
                "teams:remove_member",
                args=[self.team.pk, membership.pk],
            )
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(self.team.has_member(outsider))

    def test_cannot_remove_captain(self):
        self.login(self.captain)
        membership = self.team.membership_for(self.captain)
        resp = self.client.post(
            reverse(
                "teams:remove_member",
                args=[self.team.pk, membership.pk],
            )
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self.team.has_member(self.captain))

    def test_non_captain_cannot_remove(self):
        self.login(self.member)
        membership = self.team.membership_for(self.captain)
        resp = self.client.post(
            reverse(
                "teams:remove_member",
                args=[self.team.pk, membership.pk],
            )
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self.team.has_member(self.captain))


class InvitationTests(TeamsBase):
    def test_captain_sends_invitation(self):
        self.login(self.captain)
        resp = self.client.post(
            reverse("teams:send_invitation", args=[self.team.pk]),
            {"invitee": self.outsider.pk, "message": "join us"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            TeamInvitation.objects.filter(
                team=self.team, invitee=self.outsider
            ).exists()
        )

    def test_accept_invitation_adds_member(self):
        invitation = TeamInvitation.objects.create(
            team=self.team,
            inviter=self.captain,
            invitee=self.outsider,
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.login(self.outsider)
        resp = self.client.post(
            reverse("teams:invitation_accept", args=[invitation.pk])
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self.team.has_member(self.outsider))

        invitation.refresh_from_db()
        self.assertEqual(
            invitation.status, TeamInvitation.Status.ACCEPTED
        )

    def test_decline_invitation(self):
        invitation = TeamInvitation.objects.create(
            team=self.team,
            inviter=self.captain,
            invitee=self.outsider,
        )
        self.login(self.outsider)
        self.client.post(
            reverse("teams:invitation_decline", args=[invitation.pk])
        )
        invitation.refresh_from_db()
        self.assertEqual(
            invitation.status, TeamInvitation.Status.DECLINED
        )
        self.assertFalse(self.team.has_member(self.outsider))

    def test_cannot_accept_another_users_invitation(self):
        invitation = TeamInvitation.objects.create(
            team=self.team,
            inviter=self.captain,
            invitee=self.outsider,
        )
        self.login(self.member)
        self.client.post(
            reverse("teams:invitation_accept", args=[invitation.pk])
        )
        invitation.refresh_from_db()
        self.assertEqual(
            invitation.status, TeamInvitation.Status.PENDING
        )
        self.assertFalse(self.team.has_member(self.outsider))

    def test_cannot_accept_expired_invitation(self):
        invitation = TeamInvitation.objects.create(
            team=self.team,
            inviter=self.captain,
            invitee=self.outsider,
            expires_at=timezone.now() - timedelta(days=1),
        )
        self.login(self.outsider)
        self.client.post(
            reverse("teams:invitation_accept", args=[invitation.pk])
        )
        self.assertFalse(self.team.has_member(self.outsider))

    def test_invitation_cannot_exceed_capacity(self):
        filler = make_user("fill", P)
        add_membership(self.team, filler)
        self.assertTrue(self.team.is_full())

        invitation = TeamInvitation.objects.create(
            team=self.team,
            inviter=self.captain,
            invitee=self.outsider,
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.login(self.outsider)
        self.client.post(
            reverse("teams:invitation_accept", args=[invitation.pk])
        )
        self.assertFalse(self.team.has_member(self.outsider))

    def test_cancel_invitation_by_sender(self):
        invitation = TeamInvitation.objects.create(
            team=self.team,
            inviter=self.captain,
            invitee=self.outsider,
        )
        self.login(self.captain)
        self.client.post(
            reverse("teams:invitation_cancel", args=[invitation.pk])
        )
        invitation.refresh_from_db()
        self.assertEqual(
            invitation.status, TeamInvitation.Status.CANCELLED
        )


class JoinRequestTests(TeamsBase):
    def test_request_and_approve(self):
        self.login(self.outsider)
        self.client.post(
            reverse("teams:request_join", args=[self.team.pk])
        )
        join_request = JoinRequest.objects.get(
            team=self.team, user=self.outsider
        )
        self.assertEqual(
            join_request.status, JoinRequest.Status.PENDING
        )

        self.login(self.captain)
        self.client.post(
            reverse(
                "teams:join_request_respond",
                args=[join_request.pk],
            ),
            {"action": "accept"},
        )
        join_request.refresh_from_db()
        self.assertEqual(
            join_request.status, JoinRequest.Status.ACCEPTED
        )
        self.assertTrue(self.team.has_member(self.outsider))

    def test_reject_request(self):
        self.login(self.outsider)
        self.client.post(
            reverse("teams:request_join", args=[self.team.pk])
        )
        join_request = JoinRequest.objects.get(
            team=self.team, user=self.outsider
        )

        self.login(self.captain)
        self.client.post(
            reverse(
                "teams:join_request_respond",
                args=[join_request.pk],
            ),
            {"action": "reject"},
        )
        join_request.refresh_from_db()
        self.assertEqual(
            join_request.status, JoinRequest.Status.REJECTED
        )
        self.assertFalse(self.team.has_member(self.outsider))

    def test_join_button_creates_request_not_membership(self):
        self.login(self.outsider)
        resp = self.client.post(
            reverse("teams:request_join", args=[self.team.pk])
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(self.team.has_member(self.outsider))
        self.assertEqual(
            JoinRequest.objects.filter(
                team=self.team,
                user=self.outsider,
                status=JoinRequest.Status.PENDING,
            ).count(),
            1,
        )

    def test_duplicate_requests_blocked(self):
        self.login(self.outsider)
        self.client.post(
            reverse("teams:request_join", args=[self.team.pk])
        )
        self.client.post(
            reverse("teams:request_join", args=[self.team.pk])
        )
        self.assertEqual(
            JoinRequest.objects.filter(
                team=self.team,
                user=self.outsider,
                status=JoinRequest.Status.PENDING,
            ).count(),
            1,
        )

    def test_member_cannot_request(self):
        self.login(self.member)
        self.client.post(
            reverse("teams:request_join", args=[self.team.pk])
        )
        self.assertFalse(
            JoinRequest.objects.filter(user=self.member).exists()
        )

    def test_accept_blocked_when_full(self):
        self.login(self.outsider)
        self.client.post(
            reverse("teams:request_join", args=[self.team.pk])
        )
        join_request = JoinRequest.objects.get(
            team=self.team, user=self.outsider
        )

        filler = make_user("filler2", P)
        add_membership(self.team, filler)
        self.assertTrue(self.team.is_full())

        self.login(self.captain)
        resp = self.client.post(
            reverse(
                "teams:join_request_respond",
                args=[join_request.pk],
            ),
            {"action": "accept"},
        )
        self.assertEqual(resp.status_code, 302)
        join_request.refresh_from_db()
        self.assertEqual(
            join_request.status, JoinRequest.Status.PENDING
        )
        self.assertFalse(self.team.has_member(self.outsider))

    def test_accept_blocked_when_user_in_other_team_same_hackathon(self):
        join_request = JoinRequest.objects.create(
            team=self.team,
            user=self.outsider,
        )
        other = Team.objects.create(
            name="Rivals",
            hackathon=self.hack,
            created_by=self.member,
            captain=self.member,
            max_members=5,
        )
        add_membership(
            other, self.member, TeamMembership.Role.CAPTAIN
        )
        add_membership(other, self.outsider)

        self.login(self.captain)
        self.client.post(
            reverse(
                "teams:join_request_respond",
                args=[join_request.pk],
            ),
            {"action": "accept"},
        )
        join_request.refresh_from_db()
        self.assertEqual(
            join_request.status, JoinRequest.Status.PENDING
        )
        self.assertFalse(self.team.has_member(self.outsider))

    def test_only_captain_can_respond(self):
        self.login(self.outsider)
        self.client.post(
            reverse("teams:request_join", args=[self.team.pk])
        )
        join_request = JoinRequest.objects.get(
            team=self.team, user=self.outsider
        )

        self.login(self.member)
        resp = self.client.post(
            reverse(
                "teams:join_request_respond",
                args=[join_request.pk],
            ),
            {"action": "accept"},
        )
        self.assertEqual(resp.status_code, 302)
        join_request.refresh_from_db()
        self.assertEqual(
            join_request.status, JoinRequest.Status.PENDING
        )
        self.assertFalse(self.team.has_member(self.outsider))

    def test_requester_can_cancel(self):
        self.login(self.outsider)
        self.client.post(
            reverse("teams:request_join", args=[self.team.pk])
        )
        join_request = JoinRequest.objects.get(
            team=self.team, user=self.outsider
        )
        self.client.post(
            reverse(
                "teams:join_request_cancel",
                args=[join_request.pk],
            )
        )
        join_request.refresh_from_db()
        self.assertEqual(
            join_request.status, JoinRequest.Status.CANCELLED
        )

    def test_captain_list_shows_pending_count(self):
        self.login(self.outsider)
        self.client.post(
            reverse("teams:request_join", args=[self.team.pk])
        )
        self.login(self.captain)
        resp = self.client.get(reverse("teams:home"))
        self.assertEqual(
            resp.context["pending_join_requests"], 1
        )

    def test_direct_join_route_removed(self):
        from django.urls import NoReverseMatch

        with self.assertRaises(NoReverseMatch):
            reverse("teams:join", args=[self.team.pk])

    def test_request_blocked_on_private_team(self):
        private = Team.objects.create(
            name="Secret",
            hackathon=self.hack,
            created_by=self.captain,
            captain=self.captain,
            max_members=5,
            visibility=Team.Visibility.PRIVATE,
        )
        self.login(self.outsider)
        self.client.post(
            reverse("teams:request_join", args=[private.pk])
        )
        self.assertFalse(
            JoinRequest.objects.filter(team=private).exists()
        )

    def test_request_blocked_when_join_requests_disabled(self):
        no_requests = Team.objects.create(
            name="NoRequests",
            hackathon=self.hack,
            created_by=self.captain,
            captain=self.captain,
            max_members=5,
            allow_join_requests=False,
        )
        self.login(self.outsider)
        self.client.post(
            reverse("teams:request_join", args=[no_requests.pk])
        )
        self.assertFalse(
            JoinRequest.objects.filter(team=no_requests).exists()
        )

    def test_request_blocked_on_archived_team(self):
        archived = Team.objects.create(
            name="Archived",
            hackathon=self.hack,
            created_by=self.captain,
            captain=self.captain,
            max_members=5,
            status=Team.Status.ARCHIVED,
        )
        self.login(self.outsider)
        self.client.post(
            reverse("teams:request_join", args=[archived.pk])
        )
        self.assertFalse(
            JoinRequest.objects.filter(team=archived).exists()
        )

    def test_request_blocked_on_closed_hackathon(self):
        self.hack.status = Hackathon.Status.DRAFT
        self.hack.save(update_fields=["status"])

        self.login(self.outsider)
        self.client.post(
            reverse("teams:request_join", args=[self.team.pk])
        )
        self.assertFalse(
            JoinRequest.objects.filter(team=self.team).exists()
        )

    def test_approve_revalidates_closed_hackathon(self):
        self.login(self.outsider)
        self.client.post(
            reverse("teams:request_join", args=[self.team.pk])
        )
        join_request = JoinRequest.objects.get(
            team=self.team, user=self.outsider
        )

        self.hack.status = Hackathon.Status.DRAFT
        self.hack.save(update_fields=["status"])

        self.login(self.captain)
        self.client.post(
            reverse(
                "teams:join_request_respond",
                args=[join_request.pk],
            ),
            {"action": "accept"},
        )
        join_request.refresh_from_db()
        self.assertEqual(
            join_request.status, JoinRequest.Status.PENDING
        )
        self.assertFalse(self.team.has_member(self.outsider))

    def test_approve_revalidates_team_made_private(self):
        self.login(self.outsider)
        self.client.post(
            reverse("teams:request_join", args=[self.team.pk])
        )
        join_request = JoinRequest.objects.get(
            team=self.team, user=self.outsider
        )

        self.team.visibility = Team.Visibility.PRIVATE
        self.team.save(update_fields=["visibility"])

        self.login(self.captain)
        self.client.post(
            reverse(
                "teams:join_request_respond",
                args=[join_request.pk],
            ),
            {"action": "accept"},
        )
        join_request.refresh_from_db()
        self.assertEqual(
            join_request.status, JoinRequest.Status.PENDING
        )
        self.assertFalse(self.team.has_member(self.outsider))

    def test_approve_revalidates_join_requests_disabled(self):
        self.login(self.outsider)
        self.client.post(
            reverse("teams:request_join", args=[self.team.pk])
        )
        join_request = JoinRequest.objects.get(
            team=self.team, user=self.outsider
        )

        self.team.allow_join_requests = False
        self.team.save(update_fields=["allow_join_requests"])

        self.login(self.captain)
        self.client.post(
            reverse(
                "teams:join_request_respond",
                args=[join_request.pk],
            ),
            {"action": "accept"},
        )
        join_request.refresh_from_db()
        self.assertEqual(
            join_request.status, JoinRequest.Status.PENDING
        )
        self.assertFalse(self.team.has_member(self.outsider))


class RoleScopingTests(TeamsBase):
    def test_organizer_only_sees_own_hackathon_teams(self):
        self.login(self.org)
        resp = self.client.get(reverse("teams:home"))
        names = [team.name for team in resp.context["browse"]]
        self.assertIn("Code Warriors", names)

        self.login(self.other_org)
        resp = self.client.get(reverse("teams:home"))
        names = [team.name for team in resp.context["browse"]]
        self.assertNotIn("Code Warriors", names)

    def test_jury_cannot_join_or_invite(self):
        team2 = Team.objects.create(
            name="JuryTarget",
            hackathon=self.hack,
            created_by=self.captain,
            captain=self.captain,
            max_members=5,
            visibility=Team.Visibility.PUBLIC,
        )
        self.login(self.jury)
        self.client.post(
            reverse("teams:request_join", args=[team2.pk])
        )
        self.assertFalse(team2.has_member(self.jury))
        self.assertFalse(
            JoinRequest.objects.filter(user=self.jury).exists()
        )

        self.client.post(
            reverse("teams:send_invitation", args=[self.team.pk]),
            {"invitee": self.outsider.pk},
        )
        self.assertFalse(
            TeamInvitation.objects.filter(inviter=self.jury).exists()
        )

    def test_filter_by_search(self):
        self.login(self.captain)
        resp = self.client.get(
            reverse("teams:home") + "?q=Warriors"
        )
        self.assertContains(resp, "Code Warriors")

        resp = self.client.get(
            reverse("teams:home") + "?q=nomatch"
        )
        self.assertNotContains(resp, "Code Warriors")

    def test_disband_by_captain(self):
        self.login(self.captain)
        resp = self.client.post(
            reverse("teams:disband", args=[self.team.pk])
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(
            Team.objects.filter(pk=self.team.pk).exists()
        )

    def test_non_captain_cannot_disband(self):
        self.login(self.member)
        self.client.post(
            reverse("teams:disband", args=[self.team.pk])
        )
        self.assertTrue(
            Team.objects.filter(pk=self.team.pk).exists()
        )


class SelectForUpdateAutocommitTests(TransactionTestCase):
    """Verify that select_for_update() is used inside a transaction."""

    def setUp(self):
        self.captain = make_user("t_cap", P)
        self.invitee = make_user("t_invitee", P)
        self.requester = make_user("t_requester", P)
        organizer = make_user("t_org", O)

        self.hack = make_hackathon(
            organizer,
            title="Lock Test",
            max_team_size=5,
        )
        self.team = Team.objects.create(
            name="Lock Team",
            hackathon=self.hack,
            created_by=self.captain,
            captain=self.captain,
            max_members=5,
            allow_join_requests=True,
        )
        add_membership(
            self.team, self.captain, TeamMembership.Role.CAPTAIN
        )

    def test_accept_invitation_in_autocommit(self):
        invitation = TeamInvitation.objects.create(
            team=self.team,
            inviter=self.captain,
            invitee=self.invitee,
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.client.force_login(self.invitee)
        resp = self.client.post(
            reverse("teams:invitation_accept", args=[invitation.pk])
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self.team.has_member(self.invitee))

    def test_decline_invitation_in_autocommit(self):
        invitation = TeamInvitation.objects.create(
            team=self.team,
            inviter=self.captain,
            invitee=self.invitee,
        )
        self.client.force_login(self.invitee)
        resp = self.client.post(
            reverse("teams:invitation_decline", args=[invitation.pk])
        )
        self.assertEqual(resp.status_code, 302)

    def test_respond_join_request_in_autocommit(self):
        join_request = JoinRequest.objects.create(
            team=self.team,
            user=self.requester,
            message="me",
        )
        self.client.force_login(self.captain)
        resp = self.client.post(
            reverse(
                "teams:join_request_respond",
                args=[join_request.pk],
            ),
            {"action": "accept"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(self.team.has_member(self.requester))


class SeedDemoCleanupTests(TeamsBase):
    """``seed_teams_demo --delete`` must only remove its own demo rows."""

    def test_delete_keeps_hackathons_that_still_have_teams(self):
        from django.core.management import call_command

        # A hackathon carrying a demo title that also owns a non-demo team.
        shared = make_hackathon(self.org, title="Global AI Sprint")
        real = Team.objects.create(
            name="Not Demo Data", hackathon=shared,
            created_by=self.captain, captain=self.captain, max_members=5,
        )
        add_membership(real, self.captain, TeamMembership.Role.CAPTAIN)

        call_command("seed_teams_demo", delete=True, stdout=io.StringIO())

        shared.refresh_from_db()
        self.assertEqual(shared.title, "Global AI Sprint")
        self.assertTrue(Team.objects.filter(pk=real.pk).exists())
        # Its own demo teams are gone, and so is the rest of the demo data.
        self.assertFalse(Team.objects.filter(name="Code Warriors").exists())
        self.assertEqual(TeamMembership.objects.filter(
            team=self.team).count(), 0)

    def test_delete_removes_an_unreferenced_demo_hackathon(self):
        from django.core.management import call_command

        orphan = make_hackathon(self.org, title="GreenTech Weekend")
        call_command("seed_teams_demo", delete=True, stdout=io.StringIO())
        self.assertFalse(Hackathon.objects.filter(pk=orphan.pk).exists())

    def test_delete_leaves_other_hackathons_and_users_alone(self):
        from django.core.management import call_command

        call_command("seed_teams_demo", delete=True, stdout=io.StringIO())
        self.assertTrue(Hackathon.objects.filter(pk=self.other_hack.pk).exists())
        for user in (self.org, self.captain, self.member, self.outsider, self.jury):
            self.assertTrue(User.objects.filter(pk=user.pk).exists())


class HackathonIntegrationRegressionTests(TeamsBase):
    """Teams must gate on the *real* Hackathon business API (Abir's model).

    Regression guards for the integration break where Teams referenced Hackathon
    fields that no longer exist (``accepts_participants`` / ``allow_multiple_teams``)
    and for the method-collision that removed ``is_joinable`` from ``Team``.
    """

    def _fresh_team(self, name, **kw):
        defaults = dict(
            hackathon=self.hack,
            created_by=self.captain,
            captain=self.captain,
            max_members=5,
        )
        defaults.update(kw)
        return Team.objects.create(name=name, **defaults)

    def test_is_joinable_is_a_team_method(self):
        self.assertTrue(callable(getattr(Team, "is_joinable", None)))
        self.assertTrue(callable(getattr(Team, "add_member", None)))

    def test_published_and_open_blocks_no_joinable_team(self):
        self.assertTrue(self.hack.registration_open)
        team = self._fresh_team("OpenTeam")
        ok, reason = team.is_joinable(self.outsider)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_cancelled_hackathon_blocks_join(self):
        self.hack.status = Hackathon.Status.CANCELLED
        self.hack.save(update_fields=["status"])
        team = self._fresh_team("CancelledTeam")
        ok, reason = team.is_joinable(self.outsider)
        self.assertFalse(ok)
        self.assertIn("hackathon", reason.lower())

    def test_past_registration_deadline_blocks_join(self):
        # PUBLISHED but the registration window has closed.
        self.hack.registration_deadline = timezone.now() - timedelta(days=1)
        self.hack.save(update_fields=["registration_deadline"])
        team = self._fresh_team("LateTeam")
        self.assertFalse(team.is_joinable(self.outsider)[0])

    def test_no_hackathon_team_is_joinable_and_explicit(self):
        team = self._fresh_team("Unbound", hackathon=None)
        ok, _ = team.is_joinable(self.outsider)
        self.assertTrue(ok)
        self.login(self.outsider)
        resp = self.client.post(
            reverse("teams:request_join", args=[team.pk])
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            JoinRequest.objects.filter(team=team, user=self.outsider).exists()
        )

    def test_list_shows_hackathon_title(self):
        self.login(self.captain)
        resp = self.client.get(reverse("teams:home"))
        self.assertContains(resp, "AI Challenge")


class InvitationRestrictionTests(TeamsBase):
    """Accepting an invitation waives only the join-request opt-ins.

    Regression guards for the check that was lost when the old
    ``Hackathon.allow_multiple_teams`` flag disappeared: an invitation must not
    become a side door around capacity, the registration window, duplicate
    memberships or the one-team-per-hackathon rule.
    """

    def _invite(self, team, user, **kw):
        defaults = dict(
            team=team, inviter=team.captain, invitee=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        defaults.update(kw)
        return TeamInvitation.objects.create(**defaults)

    def _accept(self, invitation):
        self.login(invitation.invitee)
        return self.client.post(
            reverse("teams:invitation_accept", args=[invitation.pk])
        )

    def test_invitation_cannot_bypass_one_team_per_hackathon(self):
        rival = Team.objects.create(
            name="Rival Squad", hackathon=self.hack,
            created_by=self.outsider, captain=self.outsider, max_members=5,
        )
        add_membership(rival, self.outsider, TeamMembership.Role.CAPTAIN)
        invitation = self._invite(self.team, self.outsider)

        self._accept(invitation)
        invitation.refresh_from_db()
        self.assertFalse(self.team.has_member(self.outsider))
        self.assertEqual(invitation.status, TeamInvitation.Status.PENDING)
        self.assertEqual(
            TeamMembership.objects.filter(user=self.outsider,
                                          team__hackathon=self.hack).count(),
            1,
        )

    def test_invitation_blocked_when_hackathon_registration_closed(self):
        invitation = self._invite(self.team, self.outsider)
        self.hack.status = Hackathon.Status.CANCELLED
        self.hack.save(update_fields=["status"])

        self._accept(invitation)
        invitation.refresh_from_db()
        self.assertFalse(self.team.has_member(self.outsider))
        self.assertEqual(invitation.status, TeamInvitation.Status.PENDING)

    def test_invitation_blocked_when_registration_deadline_passed(self):
        invitation = self._invite(self.team, self.outsider)
        self.hack.registration_deadline = timezone.now() - timedelta(days=1)
        self.hack.save(update_fields=["registration_deadline"])

        self._accept(invitation)
        self.assertFalse(self.team.has_member(self.outsider))

    def test_invitation_blocked_on_archived_team(self):
        archived = Team.objects.create(
            name="Shelved", hackathon=self.hack,
            created_by=self.captain, captain=self.captain, max_members=5,
            status=Team.Status.ARCHIVED,
        )
        invitation = self._invite(archived, self.outsider)

        self._accept(invitation)
        self.assertFalse(archived.has_member(self.outsider))

    def test_invitation_still_works_for_private_team(self):
        private = Team.objects.create(
            name="Invite Only", hackathon=self.hack,
            created_by=self.captain, captain=self.captain, max_members=5,
            visibility=Team.Visibility.PRIVATE, allow_join_requests=False,
        )
        invitation = self._invite(private, self.outsider)

        self._accept(invitation)
        invitation.refresh_from_db()
        self.assertTrue(private.has_member(self.outsider))
        self.assertEqual(invitation.status, TeamInvitation.Status.ACCEPTED)
        self.assertIsNotNone(invitation.responded_at)

    def test_full_team_invitation_is_expired(self):
        add_membership(self.team, make_user("fillslot", P))
        self.assertTrue(self.team.is_full())
        invitation = self._invite(self.team, self.outsider)

        self._accept(invitation)
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, TeamInvitation.Status.EXPIRED)
        self.assertIsNotNone(invitation.responded_at)
        self.assertFalse(self.team.has_member(self.outsider))

    def test_accepting_as_existing_member_does_not_duplicate(self):
        invitation = self._invite(self.team, self.member)

        self._accept(invitation)
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, TeamInvitation.Status.ACCEPTED)
        self.assertEqual(
            TeamMembership.objects.filter(team=self.team,
                                          user=self.member).count(),
            1,
        )

    def test_two_invitations_in_different_hackathons_both_work(self):
        other_team = Team.objects.create(
            name="Web Squad", hackathon=self.other_hack,
            created_by=self.captain, captain=self.captain, max_members=5,
        )
        first = self._invite(self.team, self.outsider)
        self._accept(first)
        second = self._invite(other_team, self.outsider)
        self.login(self.outsider)
        self.client.post(reverse("teams:invitation_accept", args=[second.pk]))

        self.assertTrue(self.team.has_member(self.outsider))
        self.assertTrue(other_team.has_member(self.outsider))

    def test_invitation_form_rejects_user_with_team_in_same_hackathon(self):
        rival = Team.objects.create(
            name="Taken", hackathon=self.hack,
            created_by=self.outsider, captain=self.outsider, max_members=5,
        )
        add_membership(rival, self.outsider, TeamMembership.Role.CAPTAIN)
        form = InviteMemberForm({"invitee": self.outsider.pk}, team=self.team)
        self.assertFalse(form.is_valid())
        self.assertIn("already have a team",
                      str(form.errors["invitee"][0]).lower())


class TeamFormHackathonSelectorTests(TeamsBase):
    def _choices(self, form):
        return list(form.fields["hackathon"].queryset)

    def test_only_published_and_open_hackathons_are_selectable(self):
        closed = make_hackathon(self.org, title="Closed Window")
        closed.registration_deadline = timezone.now() - timedelta(days=1)
        closed.save(update_fields=["registration_deadline"])
        draft = make_hackathon(self.org, title="Draft One")
        draft.status = Hackathon.Status.DRAFT
        draft.save(update_fields=["status"])

        choices = self._choices(TeamForm())
        self.assertIn(self.hack.pk, [h.pk for h in choices])
        self.assertNotIn(closed.pk, [h.pk for h in choices])
        self.assertNotIn(draft.pk, [h.pk for h in choices])

    def test_deadline_is_evaluated_per_request_not_at_import(self):
        past = make_hackathon(self.org, title="Just Expired")
        past.registration_deadline = timezone.now() - timedelta(seconds=1)
        past.save(update_fields=["registration_deadline"])
        self.assertNotIn(past.pk, [h.pk for h in self._choices(TeamForm())])

    def test_edit_form_keeps_the_teams_own_hackathon_selectable(self):
        # A captain must still be able to edit description/capacity after the
        # hackathon stopped accepting registrations.
        self.hack.status = Hackathon.Status.CANCELLED
        self.hack.save(update_fields=["status"])
        self.login(self.captain)
        resp = self.client.post(
            reverse("teams:edit", args=[self.team.pk]),
            {"name": "Code Warriors", "description": "still editable",
             "hackathon": self.hack.pk, "max_members": 3,
             "visibility": "public", "allow_join_requests": "on"},
        )
        self.assertEqual(resp.status_code, 302)
        self.team.refresh_from_db()
        self.assertEqual(self.team.description, "still editable")

    def test_post_with_closed_hackathon_is_rejected(self):
        self.hack.status = Hackathon.Status.CANCELLED
        self.hack.save(update_fields=["status"])
        self.login(self.outsider)
        resp = self.client.post(
            reverse("teams:create"),
            {"name": "Blocked Squad", "hackathon": self.hack.pk,
             "max_members": 4, "visibility": "public"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("hackathon", resp.context["form"].errors)
        self.assertFalse(Team.objects.filter(name="Blocked Squad").exists())


class TeamModelRuleTests(TeamsBase):
    def test_is_joinable_waives_only_opt_ins_for_invitations(self):
        private = Team.objects.create(
            name="Private Opt-out", hackathon=self.hack,
            created_by=self.captain, captain=self.captain, max_members=5,
            visibility=Team.Visibility.PRIVATE, allow_join_requests=False,
        )
        ok, reason = private.is_joinable(self.outsider)
        self.assertFalse(ok)
        ok, reason = private.is_joinable(self.outsider, via_invitation=True)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_is_joinable_flags_the_one_team_rule(self):
        rival = Team.objects.create(
            name="Other Squad", hackathon=self.hack,
            created_by=self.outsider, captain=self.outsider, max_members=5,
        )
        add_membership(rival, self.outsider, TeamMembership.Role.CAPTAIN)
        ok, reason = self.team.is_joinable(self.outsider)
        self.assertFalse(ok)
        self.assertIn("already have a team", reason.lower())

    def test_unique_constraint_blocks_duplicate_membership(self):
        from django.db import IntegrityError, transaction as db_transaction

        with db_transaction.atomic():
            with self.assertRaises(IntegrityError):
                TeamMembership.objects.create(team=self.team, user=self.member)

    def test_add_member_is_idempotent(self):
        self.team.add_member(self.member)
        self.assertEqual(
            TeamMembership.objects.filter(team=self.team,
                                          user=self.member).count(),
            1,
        )


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
        # "closed" = the registration deadline has passed; no open window.
        self.hack.registration_deadline = timezone.now() - timedelta(hours=1)
        self.hack.save(update_fields=['registration_deadline'])
        self.login(self._participant('o5', skills='python'))
        resp = self.client.get(self.url(self.hack))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.context['selected_hackathon'])
        self.assertEqual(resp.context['results'], [])

    def test_unpublished_hackathon_is_not_selectable(self):
        self.hack.status = Hackathon.Status.DRAFT
        self.hack.save(update_fields=['status'])
        self.login(self._participant('o5draft', skills='python'))
        resp = self.client.get(self.url(self.hack))
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
        empty = make_hackathon(self.org, title='EmptyHack')
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


class SeedDemoHackathonRepairTests(TeamsBase):
    """``seed_teams_demo --repair-demo-hackathons`` completes only its own rows."""

    def _legacy_demo_row(self, title="Global AI Sprint", **over):
        """A hackathon as the pre-refactor seed left it: legacy status string,
        empty category/format, everything else already filled."""
        now = timezone.now()
        defaults = dict(
            organizer=self.org,
            title=title,
            description=f"Demo hackathon: {title}.",
            status="registration_open",
            category="",
            event_format="",
            start_date=now + timedelta(days=2),
            end_date=now + timedelta(days=5),
            registration_deadline=now + timedelta(days=1),
            submission_deadline=now + timedelta(days=4),
            min_team_size=2,
            max_team_size=4,
        )
        defaults.update(over)
        return Hackathon.objects.create(**defaults)

    def test_repair_publishes_and_categorises_its_own_legacy_row(self):
        row = self._legacy_demo_row()
        kept_start = row.start_date

        call_command("seed_teams_demo", repair_hackathons=True, stdout=io.StringIO())

        row.refresh_from_db()
        self.assertEqual(row.status, Hackathon.Status.PUBLISHED)
        self.assertEqual(row.category, Hackathon.Category.AI)
        self.assertEqual(row.event_format, Hackathon.Format.ONLINE)
        # A value that was already there is never replaced.
        self.assertEqual(row.start_date, kept_start)

    def test_repair_never_touches_a_hackathon_it_did_not_create(self):
        row = self._legacy_demo_row(description="Organiser notes for the real event")

        call_command("seed_teams_demo", repair_hackathons=True, stdout=io.StringIO())

        row.refresh_from_db()
        self.assertEqual(row.status, "registration_open")
        self.assertEqual(row.category, "")

    def test_repair_keeps_a_valid_status_and_needs_no_change(self):
        row = self._legacy_demo_row(status=Hackathon.Status.DRAFT,
                                    category=Hackathon.Category.WEB,
                                    event_format=Hackathon.Format.HYBRID)

        call_command("seed_teams_demo", repair_hackathons=True, stdout=io.StringIO())

        row.refresh_from_db()
        self.assertEqual(row.status, Hackathon.Status.DRAFT)
        self.assertEqual(row.category, Hackathon.Category.WEB)
        self.assertEqual(row.event_format, Hackathon.Format.HYBRID)

    def test_repair_fills_the_fields_the_diverged_dev_rows_are_missing(self):
        # The dev DB rows have no dates and no category at all because 0002
        # added those columns NULLABLE. A fresh test DB cannot express that,
        # so relax the columns for this test only (Postgres rolls the DDL back
        # with the test transaction).
        quoted = connection.ops.quote_name("hackathons_hackathon")
        with connection.cursor() as c:
            c.execute("SET CONSTRAINTS ALL IMMEDIATE")
            for column in ("start_date", "end_date", "registration_deadline",
                           "submission_deadline", "max_participants", "min_team_size"):
                c.execute(f'ALTER TABLE {quoted} ALTER COLUMN "{column}" DROP NOT NULL')
        row = self._legacy_demo_row()
        with connection.cursor() as c:
            c.execute('UPDATE {0} SET start_date=NULL, end_date=NULL, '
                      'registration_deadline=NULL, submission_deadline=NULL, '
                      'max_participants=NULL, min_team_size=NULL WHERE id=%s'.format(quoted),
                      [row.pk])

        call_command("seed_teams_demo", repair_hackathons=True, stdout=io.StringIO())

        row.refresh_from_db()
        self.assertEqual(row.status, Hackathon.Status.PUBLISHED)
        self.assertIsNotNone(row.category)
        self.assertEqual(row.min_team_size, 2)
        self.assertEqual(row.max_participants, 150)
        self.assertLess(row.registration_deadline, row.start_date)
        self.assertLess(row.start_date, row.submission_deadline)
        self.assertLess(row.submission_deadline, row.end_date)
        # The business outcome: the demo hackathon accepts participants again.
        self.assertTrue(row.registration_open)
