"""Seed realistic demo data for the Teams module.

Idempotent: safe to run repeatedly. ``--delete`` removes ONLY the demo rows this
command manages (by fixed demo identifiers), never touching existing accounts or
their passwords. Existing users kmarbenmbarek / demo / leilabouanani / abir are
reused and never deleted. No permissions are bypassed: rows are written through
the same model helpers (Team.add_member) the app uses.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from hackathons.models import Hackathon
from teams.models import JoinRequest, Team, TeamInvitation, TeamMembership

User = get_user_model()

# --- Fixed demo identifiers (used for safe cleanup) ----------------------
DEMO_TEAM_NAMES = {
    'Code Warriors', 'Green Innovators', 'Pixel Pioneers', 'Quantum Squad',
}
DEMO_HACKATHON_SLUGS = {
    'global-ai-sprint', 'greentech-weekend', 'web3-creators-cup',
}
# New participant accounts created for the demo (existing accounts excluded).
DEMO_USERNAMES = {'alice_demo', 'bob_demo', 'carol_demo', 'dave_demo'}
DEMO_PASSWORD = 'demo12345'


class Command(BaseCommand):
    help = 'Create or refresh Teams demo data (hackathons, teams, members, invitations, join requests).'

    def add_arguments(self, parser):
        parser.add_argument('--delete', action='store_true',
                            help='Remove only the demo data created by this command.')

    @transaction.atomic
    def handle(self, *args, **opts):
        if opts['delete']:
            self._delete()
            return
        self._seed()

    # ------------------------------------------------------------------
    def _delete(self):
        removed = {
            'teams': Team.objects.filter(name__in=DEMO_TEAM_NAMES).count(),
            'memberships': TeamMembership.objects.filter(team__name__in=DEMO_TEAM_NAMES).count(),
            'invitations': TeamInvitation.objects.filter(team__name__in=DEMO_TEAM_NAMES).count(),
            'join_requests': JoinRequest.objects.filter(team__name__in=DEMO_TEAM_NAMES).count(),
            'hackathons': Hackathon.objects.filter(slug__in=DEMO_HACKATHON_SLUGS).count(),
            'demo_users': User.objects.filter(username__in=DEMO_USERNAMES).count(),
        }
        # Deleting teams cascades their memberships/invitations/join requests.
        Team.objects.filter(name__in=DEMO_TEAM_NAMES).delete()
        Hackathon.objects.filter(slug__in=DEMO_HACKATHON_SLUGS).delete()
        User.objects.filter(username__in=DEMO_USERNAMES).delete()
        self.stdout.write(self.style.WARNING('Deleted demo data: %s' % removed))

    def _user(self, username, role, first, last, create):
        u = User.objects.filter(username=username).first()
        if u:
            return u
        if not create:
            return None
        u = User.objects.create_user(username=username, email=f'{username}@demo.local',
                                     password=DEMO_PASSWORD, role=role,
                                     first_name=first, last_name=last)
        return u

    @transaction.atomic
    def _seed(self):
        # Reused existing accounts (never modified).
        kmar = self._user('kmarbenmbarek', User.Role.PARTICIPANT, '', '', False)
        org_demo = self._user('demo', User.Role.ORGANIZER, '', '', False)
        org_leila = self._user('leilabouanani', User.Role.ORGANIZER, '', '', False)

        # New demo participants (created only if absent).
        alice = self._user('alice_demo', User.Role.PARTICIPANT, 'Alice', 'Ben Salah', True)
        bob = self._user('bob_demo', User.Role.PARTICIPANT, 'Bob', 'Trabelsi', True)
        carol = self._user('carol_demo', User.Role.PARTICIPANT, 'Carol', 'Gharbi', True)
        dave = self._user('dave_demo', User.Role.PARTICIPANT, 'Dave', 'Mansour', True)

        # --- Hackathons ---
        h_ai = self._hackathon('Global AI Sprint', 'global-ai-sprint',
                               Hackathon.Status.REGISTRATION_OPEN, 4, False, org_demo)
        h_green = self._hackathon('GreenTech Weekend', 'greentech-weekend',
                                  Hackathon.Status.ONGOING, 3, False, org_leila)
        h_web3 = self._hackathon('Web3 Creators Cup', 'web3-creators-cup',
                                 Hackathon.Status.REGISTRATION_OPEN, 5, True, org_demo)

        # --- Teams ---
        # 1) Incomplete + recruiting, public, pending join request + pending invite.
        code = self._team('Code Warriors', h_ai, alice, max_members=4)
        self._member(code, alice, TeamMembership.Role.CAPTAIN)
        self._member(code, bob)
        if kmar:
            self._member(code, kmar)
        self._join_request(code, carol)
        self._invitation(code, alice, dave, message='We need your backend skills!')

        # 2) Full team (3/3).
        green = self._team('Green Innovators', h_green, bob, max_members=3)
        self._member(green, bob, TeamMembership.Role.CAPTAIN)
        self._member(green, carol)
        self._member(green, dave)

        # 3) Multi-member, hackathon allows multiple teams, pending invitation to alice.
        pixel = self._team('Pixel Pioneers', h_web3, carol, max_members=5)
        self._member(pixel, carol, TeamMembership.Role.CAPTAIN)
        if kmar:
            self._member(pixel, kmar)
        self._invitation(pixel, carol, alice, message='Join our Web3 squad.')

        # 4) Private team, single member.
        quantum = self._team('Quantum Squad', h_ai, dave, max_members=4,
                             visibility=Team.Visibility.PRIVATE)
        self._member(quantum, dave, TeamMembership.Role.CAPTAIN)

        # 5) Expired invitation on the full team (kmar invited to H2 where he has no team).
        if kmar:
            self._invitation(green, bob, kmar,
                             message='This invitation has expired.',
                             expired=True)

        self.stdout.write(self.style.SUCCESS(
            'Demo data ready.\n'
            '  Hackathons: %d | Teams: %d | Memberships: %d | Invitations: %d | Join requests: %d\n'
            '  Demo participant logins (password "%s"): alice_demo, bob_demo, carol_demo, dave_demo'
            % (Hackathon.objects.filter(slug__in=DEMO_HACKATHON_SLUGS).count(),
               Team.objects.filter(name__in=DEMO_TEAM_NAMES).count(),
               TeamMembership.objects.filter(team__name__in=DEMO_TEAM_NAMES).count(),
               TeamInvitation.objects.filter(team__name__in=DEMO_TEAM_NAMES).count(),
               JoinRequest.objects.filter(team__name__in=DEMO_TEAM_NAMES).count(),
               DEMO_PASSWORD)
        ))

    # --- helpers (get_or_create keyed on unique fields) ----------------
    def _hackathon(self, name, slug, status, max_size, multi, owner):
        h = Hackathon.objects.filter(slug=slug).first()
        if h:
            return h
        return Hackathon.objects.create(
            name=name, slug=slug, status=status, max_team_size=max_size,
            allow_multiple_teams=multi, created_by=owner,
            description=f'Demo hackathon: {name}.',
        )

    def _team(self, name, hackathon, captain, max_members=5, visibility=Team.Visibility.PUBLIC):
        team = Team.objects.filter(name=name).first()
        if team:
            return team
        return Team.objects.create(
            name=name, hackathon=hackathon, created_by=captain, captain=captain,
            max_members=max_members, visibility=visibility, allow_join_requests=True,
            description=f'Demo team {name}.',
        )

    def _member(self, team, user, role=None):
        return team.add_member(user, role=role)

    def _invitation(self, team, inviter, invitee, message='', expired=False):
        inv = TeamInvitation.objects.filter(team=team, invitee=invitee).first()
        if inv:
            return inv
        delta = timedelta(days=-1) if expired else timedelta(days=7)
        return TeamInvitation.objects.create(
            team=team, inviter=inviter, invitee=invitee, message=message,
            status=TeamInvitation.Status.PENDING,
            expires_at=timezone.now() + delta,
        )

    def _join_request(self, team, user):
        jr = JoinRequest.objects.filter(team=team, user=user,
                                        status=JoinRequest.Status.PENDING).first()
        if jr:
            return jr
        return JoinRequest.objects.create(
            team=team, user=user, status=JoinRequest.Status.PENDING,
            message='I would love to contribute to this project.',
        )
