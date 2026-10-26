"""Seed realistic demo data for the Teams module.

Idempotent: safe to run repeatedly. ``--delete`` removes ONLY the demo rows this
command manages (by fixed demo identifiers), never touching existing accounts or
their passwords. ``--repair-demo-hackathons`` completes the business fields of
the demo hackathons that pre-date the current Hackathon model; it only fills
what is still empty on rows this command owns. Existing users
kmarbenmbarek / demo / leilabouanani / abir are
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
DEMO_HACKATHON_TITLES = {
    'Global AI Sprint', 'GreenTech Weekend', 'Web3 Creators Cup',
}
# The category each demo hackathon is about. Single source of truth: both the
# seed and --repair-demo-hackathons read it, so they can never disagree.
DEMO_HACKATHON_CATEGORIES = {
    'Global AI Sprint': Hackathon.Category.AI,
    'GreenTech Weekend': Hackathon.Category.GREENTECH,
    'Web3 Creators Cup': Hackathon.Category.WEB,
}
# New participant accounts created for the demo (existing accounts excluded).
DEMO_USERNAMES = {'alice_demo', 'bob_demo', 'carol_demo', 'dave_demo'}
DEMO_PASSWORD = 'demo12345'


class Command(BaseCommand):
    help = 'Create or refresh Teams demo data (hackathons, teams, members, invitations, join requests).'

    def add_arguments(self, parser):
        parser.add_argument('--delete', action='store_true',
                            help='Remove only the demo data created by this command.')
        parser.add_argument('--repair-demo-hackathons', action='store_true',
                            dest='repair_hackathons',
                            help='Fill the business fields this command left unset on its own '
                                 'demo hackathons (legacy status, no dates). Values already set '
                                 'by a person are never overwritten, and a hackathon this '
                                 'command did not create is never touched.')

    @transaction.atomic
    def handle(self, *args, **opts):
        if opts['delete']:
            self._delete()
            return
        if opts['repair_hackathons']:
            self._repair_demo_hackathons()
            return
        self._seed()

    # ------------------------------------------------------------------
    def _delete(self):
        removed = {
            'teams': Team.objects.filter(name__in=DEMO_TEAM_NAMES).count(),
            'memberships': TeamMembership.objects.filter(team__name__in=DEMO_TEAM_NAMES).count(),
            'invitations': TeamInvitation.objects.filter(team__name__in=DEMO_TEAM_NAMES).count(),
            'join_requests': JoinRequest.objects.filter(team__name__in=DEMO_TEAM_NAMES).count(),
            'demo_users': User.objects.filter(username__in=DEMO_USERNAMES).count(),
        }
        # Deleting teams cascades their memberships/invitations/join requests.
        Team.objects.filter(name__in=DEMO_TEAM_NAMES).delete()
        # A demo hackathon is dropped only once nothing references it, so a real
        # hackathon that happens to share one of these titles is never deleted.
        kept, dropped = [], []
        for hackathon in Hackathon.objects.filter(title__in=DEMO_HACKATHON_TITLES):
            if hackathon.teams.exists():
                kept.append(hackathon.title)
            else:
                dropped.append(hackathon.title)
                hackathon.delete()
        User.objects.filter(username__in=DEMO_USERNAMES).delete()
        removed['hackathons'] = dropped
        if kept:
            removed['hackathons_kept'] = kept
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
        h_ai = self._hackathon('Global AI Sprint',
                               Hackathon.Status.PUBLISHED, 4, org_demo)
        h_green = self._hackathon('GreenTech Weekend',
                                  Hackathon.Status.PUBLISHED, 3, org_leila)
        h_web3 = self._hackathon('Web3 Creators Cup',
                                 Hackathon.Status.PUBLISHED, 5, org_demo)

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

        # 3) Multi-member public team with a pending invitation to alice. kmar is in
        # Code Warriors too, which is legal: that team is in another hackathon.
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
            % (Hackathon.objects.filter(title__in=DEMO_HACKATHON_TITLES).count(),
               Team.objects.filter(name__in=DEMO_TEAM_NAMES).count(),
               TeamMembership.objects.filter(team__name__in=DEMO_TEAM_NAMES).count(),
               TeamInvitation.objects.filter(team__name__in=DEMO_TEAM_NAMES).count(),
               JoinRequest.objects.filter(team__name__in=DEMO_TEAM_NAMES).count(),
               DEMO_PASSWORD)
        ))

    # --- helpers (get_or_create keyed on unique fields) ----------------
    def _hackathon(self, title, status, max_size, owner):
        h = Hackathon.objects.filter(title=title).first()
        if h:
            return h
        now = timezone.now()
        return Hackathon.objects.create(
            title=title, organizer=owner, status=status,
            category=DEMO_HACKATHON_CATEGORIES[title],
            event_format=Hackathon.Format.ONLINE,
            description=self._demo_description(title),
            start_date=now + timedelta(days=2),
            end_date=now + timedelta(days=5),
            registration_deadline=now + timedelta(days=1),
            submission_deadline=now + timedelta(days=4),
            min_team_size=2, max_team_size=max_size,
        )

    @staticmethod
    def _demo_description(title):
        """The description this command writes for its own demo hackathons.

        It is also the ownership proof used by --repair-demo-hackathons: a
        hackathon created anywhere else cannot carry this exact text.
        """
        return f'Demo hackathon: {title}.'

    # ------------------------------------------------------------------
    @transaction.atomic
    def _repair_demo_hackathons(self):
        """Complete the demo hackathons that pre-date the current Hackathon
        model (legacy status strings, NULL business fields added by migration
        0002) so registration and team membership work on them again.

        Scope is provably limited to rows this command created: the title is a
        demo title AND the description is the demo text this command writes.
        Nothing already filled in is touched — in particular the organizer is
        never reassigned and dates set by a person are never replaced, so this
        cannot rewrite a real hackathon's schedule.
        """
        valid_statuses = {value for value, _label in Hackathon.Status.choices}
        now = timezone.now()
        repaired, skipped = [], []
        for hackathon in Hackathon.objects.filter(title__in=DEMO_HACKATHON_TITLES).order_by('id'):
            if hackathon.description != self._demo_description(hackathon.title):
                skipped.append(f'{hackathon.title} (not created by this command)')
                continue
            filled = []
            if hackathon.status not in valid_statuses:
                # Both legacy values ('registration_open', 'ongoing') describe a
                # live, visible event, which is exactly what 'published' means.
                hackathon.status = Hackathon.Status.PUBLISHED
                filled.append('status')
            if not hackathon.category:
                hackathon.category = DEMO_HACKATHON_CATEGORIES[hackathon.title]
                filled.append('category')
            if not hackathon.event_format:
                hackathon.event_format = Hackathon.Format.ONLINE
                filled.append('event_format')
            if hackathon.min_team_size is None:
                hackathon.min_team_size = Hackathon._meta.get_field('min_team_size').default
                filled.append('min_team_size')
            if hackathon.max_participants is None:
                hackathon.max_participants = Hackathon._meta.get_field('max_participants').default
                filled.append('max_participants')
            # The demo schedule is relative to "now", like a fresh seed's; a
            # row that already has real dates keeps them.
            dates = {
                'registration_deadline': now + timedelta(days=1),
                'start_date': now + timedelta(days=2),
                'submission_deadline': now + timedelta(days=4),
                'end_date': now + timedelta(days=5),
            }
            for field, value in dates.items():
                if getattr(hackathon, field) is None:
                    setattr(hackathon, field, value)
                    filled.append(field)
            if not filled:
                skipped.append(f'{hackathon.title} (already complete)')
                continue
            hackathon.save()
            repaired.append(f'{hackathon.title}: {", ".join(filled)}')
        self.stdout.write(self.style.SUCCESS(
            'Repaired demo hackathons:\n  ' + ('\n  '.join(repaired) or 'none')))
        if skipped:
            self.stdout.write(self.style.WARNING(
                'Left untouched:\n  ' + '\n  '.join(skipped)))

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
