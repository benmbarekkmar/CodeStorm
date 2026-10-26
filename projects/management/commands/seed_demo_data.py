"""Seed demo data for Projects, Evaluations and Prizes on top of existing rows.

Additive only: nothing that already exists is edited or deleted, no existing
account or password is touched, and no membership is invented -- the demo
projects hang on the teams ``seed_teams_demo`` already created, so the
team/hackathon rule is satisfied instead of bypassed.

Every write goes through the model or service the application itself uses:
``Project.full_clean``, ``Evaluation.save`` (which owns the status/is_complete
link), ``prizes.services.compute_leaderboard`` + ``save_leaderboard`` for the
ranking, and ``validate_prize_awards`` before a prize is left marked assigned.
The command cannot therefore leave data the app would reject.

Scores stay whole numbers because that is what the jury widget can store and
redisplay: ``evaluation_detail`` sends ``int(score)`` to the slider and
``evaluation_submit`` coerces with ``int(score_val)``, so a seeded 8.50 would be
silently rewritten to 8 the first time a juré touches the sheet.

Rankings are prepared but never published: ``results_published`` stays False, so
no invented "official results" reach participants. ``--dry-run`` rolls the whole
transaction back; ``--delete`` removes only rows carrying the demo markers
below. No AI report is created here: a seeded fixture must never look like a
real Gemini call.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from evaluations.models import (
    Evaluation,
    EvaluationComment,
    EvaluationCriterion,
    EvaluationScore,
)
from hackathons.models import Hackathon
from prizes.models import LeaderboardEntry, Prize, PrizeAward, PrizeCategory, Sponsor
from prizes.services import (
    LeaderboardNotReady,
    compute_leaderboard,
    save_leaderboard,
    validate_prize_awards,
    validate_saved_leaderboard,
)
from projects.models import Project
from teams.models import Team

User = get_user_model()

DEMO_PASSWORD = 'demo12345'
DEMO_NOTE = 'Demo data created by seed_demo_data.'

# The hackathon whose ranking this command prepares, and its demo prizes.
RANKED_HACKATHON = 'Global AI Sprint'

RUBRIC = (
    ('Innovation', 'How new the idea and the approach are.', Decimal('40.00')),
    ('Technical implementation', 'Code quality, architecture and completeness.', Decimal('40.00')),
    ('Impact', 'Usefulness of the result and its reach.', Decimal('20.00')),
)
RUBRIC_NAMES = {name for name, _description, _weight in RUBRIC}

# team, title, tagline, status, repo, technologies, progress
PROJECTS = (
    ('Code Warriors', 'Coach Board', 'A coaching board that turns hackathon goals into tasks.',
     Project.Status.EVALUATED, 'https://github.com/octocat/Hello-World', 'Python, Django, HTMX', 100),
    ('Quantum Squad', 'Signal Desk', 'Live signal desk for judging a hackathon in one view.',
     Project.Status.EVALUATED, 'https://github.com/octocat/Spoon-Knife',
     'TypeScript, Node.js, WebSockets', 100),
    ('Green Innovators', 'Carbon Compass', 'Carbon estimate for the stack a team chooses.',
     Project.Status.SUBMITTED, '', 'React, FastAPI', 90),
    ('Pixel Pioneers', 'Chain Notary', 'Notary trail for the documents a team shares.',
     Project.Status.IN_PROGRESS, '', 'Solidity, React', 40),
)
DEMO_PROJECT_TITLES = {row[1] for row in PROJECTS}

# project title -> jury username -> criterion name -> integer score (0..10).
# A jury with fewer criteria than the rubric stays an unfinished draft on purpose.
SCORES = {
    'Coach Board': {
        'abir': {'Innovation': 9, 'Technical implementation': 8, 'Impact': 8},
        'jury1_demo': {'Innovation': 8, 'Technical implementation': 8, 'Impact': 9},
    },
    'Signal Desk': {
        'abir': {'Innovation': 7, 'Technical implementation': 7, 'Impact': 6},
        'jury2_demo': {'Innovation': 6, 'Technical implementation': 7, 'Impact': 7},
    },
    'Carbon Compass': {
        'abir': {'Innovation': 7, 'Technical implementation': 6},
    },
    'Chain Notary': {},
}
# Shown as "to start" on the jury dashboard: an Evaluation row without any score.
PENDING_ROW = 'Chain Notary'

CATEGORIES = (
    ('Grand Prize', 'trophy', 1),
    ('Best Innovation', 'lightbulb', 2),
    ('Community Favorite', 'votes', 3),
)
DEMO_SPONSOR = 'Demo Cloud'
DEMO_PRIZE_NAMES = ('Grand Prize', 'Best Innovation', 'Community Favorite')
JURY_USERNAMES = ('jury1_demo', 'jury2_demo')
DEMO_EMAIL_SUFFIX = '@demo.local'


class Command(BaseCommand):
    help = ('Create demo Projects, Evaluations and Prizes on the existing teams. '
            'Additive only: existing rows are never edited or deleted.')

    def add_arguments(self, parser):
        parser.add_argument('--delete', action='store_true',
                            help='Remove only the demo rows this command manages.')
        parser.add_argument('--dry-run', action='store_true', dest='dry_run',
                            help='Report what would be created, then roll the transaction back.')

    @transaction.atomic
    def handle(self, *args, **opts):
        if opts['delete']:
            self._delete()
            return
        self._check_prerequisites()
        hackathons = self._hackathons()
        juries = self._juries()
        criteria = self._criteria(hackathons)
        projects = self._projects()
        self._evaluations(projects, criteria, juries)
        ranked = self._leaderboard(hackathons[RANKED_HACKATHON])
        self._prizes(hackathons[RANKED_HACKATHON], ranked)
        self._summary()
        if opts['dry_run']:
            transaction.set_rollback(True)
            self.stdout.write(self.style.WARNING('--dry-run: nothing was committed.'))

    # ---- guards ------------------------------------------------------
    def _check_prerequisites(self):
        """Refuse to build projects on a hackathon the model still rejects.

        Without a current status and a real schedule every page built on this
        data would show a legacy status string and 'Sans date prévue'.
        """
        valid = {value for value, _label in Hackathon.Status.choices}
        problems = []
        for team_name, _title, _tagline, _status, _repo, _tech, _progress in PROJECTS:
            team = Team.objects.filter(name=team_name).select_related('hackathon').first()
            if team is None:
                problems.append('team %s is missing (run seed_teams_demo first)' % team_name)
                continue
            hackathon = team.hackathon
            if hackathon is None:
                problems.append('team %s is not registered for a hackathon' % team_name)
                continue
            if hackathon.status not in valid:
                problems.append('%s: status %r is a legacy value'
                                % (hackathon.title, hackathon.status))
            if not hackathon.category or not hackathon.event_format:
                problems.append('%s: category or format is still empty' % hackathon.title)
            if not (hackathon.start_date and hackathon.end_date
                    and hackathon.registration_deadline and hackathon.submission_deadline):
                problems.append('%s: the schedule is still incomplete' % hackathon.title)
            if team.captain_id is None:
                problems.append('team %s has no captain to submit the project' % team_name)
        ranked = Hackathon.objects.filter(title=RANKED_HACKATHON).first()
        if ranked:
            foreign = LeaderboardEntry.objects.filter(hackathon=ranked).exclude(
                project__title__in=DEMO_PROJECT_TITLES)
            if foreign.exists():
                # save_leaderboard replaces the whole draft ranking of a
                # hackathon; that is the organizer's work, not this command's.
                problems.append('%s already has a prepared ranking built from other '
                                'projects; leave it to its owner' % ranked.title)
        if problems:
            raise CommandError(
                'Fix these first, then run this command again:\n  %s\n'
                'For the demo hackathons: manage.py seed_teams_demo --repair-demo-hackathons'
                % '\n  '.join(sorted(set(problems)))
            )

    def _hackathons(self):
        wanted = {row[0] for row in PROJECTS}
        hackathons = {}
        for team in Team.objects.filter(name__in=wanted).select_related('hackathon'):
            if team.hackathon_id:
                hackathons[team.hackathon.title] = team.hackathon
        return hackathons

    # ---- rows --------------------------------------------------------
    def _juries(self):
        created, reused = [], []
        for username in JURY_USERNAMES:
            if User.objects.filter(username=username).exists():
                reused.append(username)
                continue
            User.objects.create_user(
                username=username, email='%s%s' % (username, DEMO_EMAIL_SUFFIX),
                password=DEMO_PASSWORD, role=User.Role.JURY,
                first_name='Jury', last_name=username.split('_')[0].capitalize(),
            )
            created.append(username)
        self.stdout.write('Jury accounts: created %s | reused %s'
                          % (created or 'none', reused or 'none'))
        return {user.username: user for user in
                User.objects.filter(username__in=JURY_USERNAMES + ('abir',))}

    def _criteria(self, hackathons):
        """Use an organizer's rubric as it stands; only fill a missing one.

        Adding a criterion to a rubric someone defined would change how every
        existing evaluation of that hackathon is scored, so a partial match is
        reported instead of patched over.
        """
        out = {}
        for title, hackathon in sorted(hackathons.items()):
            existing = list(EvaluationCriterion.objects.filter(hackathon=hackathon))
            if existing:
                missing = sorted(RUBRIC_NAMES - {c.name for c in existing})
                if missing:
                    raise CommandError(
                        '%s already has a rubric without %s. Align the demo rubric '
                        'with the organizer\'s one instead of adding criteria to it.'
                        % (title, ', '.join(missing))
                    )
                for criterion in existing:
                    out[(title, criterion.name)] = criterion
                continue
            for order, (name, description, weight) in enumerate(RUBRIC, start=1):
                criterion = EvaluationCriterion.objects.create(
                    hackathon=hackathon, name=name, order=order, weight=weight,
                    description='%s %s' % (DEMO_NOTE, description),
                )
                out[(title, name)] = criterion
                self.stdout.write('  criterion  %-20s %s  %s%%' % (title, name, weight))
        return out

    def _projects(self):
        """One demo project per demo team, never onto a team's real submission.

        ``unique_together (team, hackathon)`` means a team can have only one
        project: a team that already submitted gets its own row reported as
        skipped rather than a second project or a rewrite of the real one.
        """
        out = {}
        submitted_at = timezone.now() - timedelta(hours=6)
        for team_name, title, tagline, status, repo_url, technologies, progress in PROJECTS:
            team = Team.objects.select_related('hackathon', 'captain').get(name=team_name)
            project = Project.objects.filter(title=title, team=team).first()
            created = False
            if project is None:
                occupant = Project.objects.filter(team=team, hackathon=team.hackathon).first()
                if occupant is not None:
                    self.stdout.write(self.style.WARNING(
                        'skipped   %-14s team %s already submitted "%s"'
                        % (title, team_name, occupant.title)))
                    continue
                project = Project.objects.create(
                    team=team, hackathon=team.hackathon, title=title, tagline=tagline,
                    description='%s Built by %s for %s.' % (DEMO_NOTE, team.name,
                                                            team.hackathon.title),
                    submitted_by=team.captain, status=status, technologies=technologies,
                    progress=progress, repo_url=repo_url,
                    submitted_at=submitted_at if status in Project.RANKED_STATUSES else None,
                )
                # Prove the stored row satisfies the team/hackathon rule instead
                # of trusting that the write happened to be consistent.
                project.full_clean()
                created = True
            out[title] = project
            self.stdout.write('%s project    %-14s %-12s (team %s)'
                              % ('created' if created else 'kept    ',
                                 title, status, team_name))
        return out

    def _evaluations(self, projects, criteria, juries):
        for title, by_jury in SCORES.items():
            project = projects.get(title)
            if project is None:
                continue
            for username, rows in by_jury.items():
                complete = len(rows) == len(RUBRIC)
                evaluation, _created = Evaluation.objects.get_or_create(
                    project=project, jury=juries[username],
                    defaults={
                        'status': Evaluation.Status.COMPLETED if complete
                                  else Evaluation.Status.IN_PROGRESS,
                        'comment': 'Clear demo, scope respected.' if complete else '',
                        'strengths': 'The core flow works end to end.' if complete else '',
                        'improvements': 'Tests and error states need depth.' if complete else '',
                        'is_complete': complete,
                    },
                )
                for name, value in rows.items():
                    EvaluationScore.objects.update_or_create(
                        evaluation=evaluation,
                        criterion=criteria[(project.hackathon.title, name)],
                        defaults={'score': Decimal(value)},
                    )
                self.stdout.write('  evaluation %-14s %-12s %s/%s scores complete=%s'
                                  % (title, username, len(rows), len(RUBRIC),
                                     evaluation.is_complete))
        pending_project = projects.get(PENDING_ROW)
        if pending_project is not None:
            Evaluation.objects.get_or_create(
                project=pending_project, jury=juries['abir'],
                defaults={'status': Evaluation.Status.PENDING, 'is_complete': False},
            )
        coach_board = projects.get('Coach Board')
        if coach_board is not None:
            discussable = (Evaluation.objects
                           .filter(project=coach_board, is_complete=True)
                           .select_related('project', 'jury')
                           .first())
            if discussable:
                EvaluationComment.objects.get_or_create(
                    evaluation=discussable,
                    text='%s Can you confirm the technical score?' % DEMO_NOTE,
                    defaults={'author': discussable.project.hackathon.organizer},
                )

    def _leaderboard(self, hackathon):
        """The prizes module computes the ranking; no rank is written by hand."""
        try:
            ranked = compute_leaderboard(hackathon)
        except LeaderboardNotReady as exc:
            raise CommandError('Ranking for %s refused: %s' % (hackathon.title, exc))
        save_leaderboard(hackathon, ranked)
        validate_saved_leaderboard(hackathon, ranked)
        for item in ranked:
            self.stdout.write('  rank %s     %-14s %s/10'
                              % (item['rank'], item['project'].title, item['score']))
        hackathon.refresh_from_db()
        if hackathon.results_published:
            raise CommandError('%s already has published results; left untouched.'
                               % hackathon.title)
        return ranked

    def _prizes(self, hackathon, ranked):
        winner = next((item['project'] for item in ranked if item['rank'] == 1), None)
        sponsor, _ = Sponsor.objects.get_or_create(
            name=DEMO_SPONSOR,
            defaults={'description': '%s Sponsor of the demo prizes.' % DEMO_NOTE,
                      'website': 'https://example.com',
                      'contact_email': 'sponsor%s' % DEMO_EMAIL_SUFFIX,
                      'created_by': hackathon.organizer},
        )
        plans = (
            ('Grand Prize', winner, Prize.Status.ASSIGNED, Decimal('3000.00')),
            ('Best Innovation', None, Prize.Status.AVAILABLE, Decimal('1000.00')),
            ('Community Favorite', None, Prize.Status.AVAILABLE, Decimal('500.00')),
        )
        icons = {name: icon for name, icon, _order in CATEGORIES}
        orders = {name: order for name, _icon, order in CATEGORIES}
        for name, winning_project, status, amount in plans:
            # A category that already exists is reused as it stands; only a
            # category this command creates carries the demo marker.
            category = PrizeCategory.objects.filter(name=name).first()
            if category is None:
                category = PrizeCategory.objects.create(
                    name=name, icon=icons[name], order=orders[name],
                    description='%s %s' % (DEMO_NOTE, name),
                )
            prize, created = Prize.objects.get_or_create(
                hackathon=hackathon, name='%s — %s' % (name, hackathon.title),
                defaults={
                    'description': '%s Awarded during %s.' % (DEMO_NOTE, hackathon.title),
                    'category': category,
                    'sponsor': sponsor,
                    'reward': '%s cloud credits' % amount,
                    'amount': amount,
                    'winning_project': winning_project,
                    'winner_slots': 1,
                    'status': status,
                    'eligibility_criteria': 'Every team registered for this hackathon.',
                },
            )
            if winning_project is not None:
                # Prize.winning_project alone is a legacy shape the validator
                # rejects, so the award row is what makes the winner real.
                prize.winning_project = winning_project
                prize.status = status
                prize.save()
                award, _ = PrizeAward.objects.get_or_create(
                    prize=prize, project=winning_project, defaults={'position': 1})
                award.full_clean()
            self.stdout.write('%s prize       %-16s %s%s'
                              % ('created' if created else 'kept    ', name, status,
                                 ' -> %s' % winning_project.title if winning_project else ''))
        try:
            validate_prize_awards(hackathon)
        except LeaderboardNotReady as exc:
            raise CommandError('Prize validation refused: %s' % exc)

    def _summary(self):
        hackathon = Hackathon.objects.get(title=RANKED_HACKATHON)
        self.stdout.write(self.style.SUCCESS(
            'Demo data ready.\n'
            '  Projects %(p)s | Criteria %(c)s | Evaluations %(e)s | Scores %(s)s | '
            'Comments %(cm)s\n'
            '  Leaderboard %(l)s entries for %(h)s (results_published=%(pub)s)\n'
            '  Prizes %(z)s | Awards %(aw)s | Categories %(k)s | Sponsors %(sp)s\n'
            '  Jury logins (password "%(pw)s"): abir, jury1_demo, jury2_demo\n'
            '  Still in the base: %(t)s teams, %(u)s users, %(h2)s hackathons'
            % {
                'p': Project.objects.count(),
                'c': EvaluationCriterion.objects.count(),
                'e': Evaluation.objects.count(),
                's': EvaluationScore.objects.count(),
                'cm': EvaluationComment.objects.count(),
                'l': LeaderboardEntry.objects.filter(hackathon=hackathon).count(),
                'h': hackathon.title,
                'pub': hackathon.results_published,
                'z': Prize.objects.count(),
                'aw': PrizeAward.objects.count(),
                'k': PrizeCategory.objects.count(),
                'sp': Sponsor.objects.count(),
                'pw': DEMO_PASSWORD,
                't': Team.objects.count(),
                'u': User.objects.count(),
                'h2': Hackathon.objects.count(),
            }))

    # ---- cleanup -----------------------------------------------------
    @transaction.atomic
    def _delete(self):
        """Remove only what this command owns, keyed on its demo markers.

        A reused prize category, a pre-existing rubric or a ranking that still
        references non-demo projects belongs to someone else, so those rows are
        reported and left alone.
        """
        hackathon = Hackathon.objects.filter(title=RANKED_HACKATHON).first()
        projects = Project.objects.filter(title__in=DEMO_PROJECT_TITLES)
        evaluations = Evaluation.objects.filter(project__in=projects)
        prizes = (Prize.objects.filter(hackathon=hackathon)
                  .filter(name__in=['%s — %s' % (n, hackathon.title)
                                    for n in DEMO_PRIZE_NAMES])
                  if hackathon else Prize.objects.none())
        if hackathon and hackathon.results_published:
            raise CommandError('%s results are published; the ranking is left alone.'
                               % hackathon.title)
        own_ranking = hackathon is not None and not LeaderboardEntry.objects.filter(
            hackathon=hackathon).exclude(project__in=projects).exists()
        demo_criteria = EvaluationCriterion.objects.filter(description__startswith=DEMO_NOTE)
        demo_categories = PrizeCategory.objects.filter(description__startswith=DEMO_NOTE)
        demo_juries = User.objects.filter(username__in=JURY_USERNAMES,
                                          email__endswith=DEMO_EMAIL_SUFFIX)
        counts = {
            'leaderboard (kept if other projects are ranked)':
                0 if own_ranking else LeaderboardEntry.objects.filter(
                    hackathon=hackathon).count(),
            'awards': PrizeAward.objects.filter(prize__in=prizes).count(),
            'scores': EvaluationScore.objects.filter(evaluation__in=evaluations).count(),
            'comments': EvaluationComment.objects.filter(evaluation__in=evaluations).count(),
            'evaluations': evaluations.count(),
            'projects': projects.count(),
            'criteria': demo_criteria.count(),
            'prizes': prizes.count(),
            'categories': demo_categories.count(),
            'sponsors': Sponsor.objects.filter(name=DEMO_SPONSOR).count(),
            'jury_users': demo_juries.count(),
        }
        PrizeAward.objects.filter(prize__in=prizes).delete()
        prizes.delete()
        if hackathon and own_ranking:
            LeaderboardEntry.objects.filter(hackathon=hackathon).delete()
        EvaluationScore.objects.filter(evaluation__in=evaluations).delete()
        EvaluationComment.objects.filter(evaluation__in=evaluations).delete()
        evaluations.delete()
        projects.delete()
        demo_criteria.delete()
        demo_categories.delete()
        Sponsor.objects.filter(name=DEMO_SPONSOR).delete()
        demo_juries.delete()
        self.stdout.write(self.style.WARNING('Removed demo rows: %s' % counts))
