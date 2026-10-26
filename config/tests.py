"""Tests des tableaux de bord et du fil de notifications.

Le contrat verifie ici est celui pose par l'audit : tout ce qu'affiche un
tableau de bord vient d'une requete, un compteur bouge quand les donnees
changent, une donnee privee d'une autre equipe n'apparait jamais, et le fil
d'notifications ne parle que de faits encore en attente.
"""
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django.views.defaults import server_error

from accounts.models import User
from config import dashboards
from evaluations.models import Evaluation, EvaluationCriterion, EvaluationScore
from hackathons.models import Hackathon
from prizes.models import LeaderboardEntry
from projects.models import Project
from teams.models import JoinRequest, Team, TeamInvitation, TeamMembership

from evaluations.tests import make_hackathon, make_project


def make_team(hackathon, captain, name, max_members=5):
    """Mirror the real creation path: teams.views adds the captain as a member."""
    team = Team.objects.create(
        name=name, hackathon=hackathon, created_by=captain, captain=captain,
        max_members=max_members,
    )
    team.add_member(captain, role=TeamMembership.Role.CAPTAIN)
    return team


def card(context, label):
    return {item['label']: item['value'] for item in context['stats_cards']}[label]


def feed_titles(user):
    return [group['title'] for group in dashboards.notification_groups(user)[0]]


def feed_texts(user):
    return [
        item['text']
        for group in dashboards.notification_groups(user)[0]
        for item in group['items']
    ]


class DashboardDataTests(TestCase):
    def setUp(self):
        self.organizer = User.objects.create_user(
            username='organizer', password='test-password', role=User.Role.ORGANIZER,
        )
        self.other_organizer = User.objects.create_user(
            username='other-organizer', password='test-password',
            role=User.Role.ORGANIZER,
        )
        self.participant = User.objects.create_user(
            username='participant', password='test-password', role=User.Role.PARTICIPANT,
        )
        self.teammate = User.objects.create_user(
            username='teammate', password='test-password', role=User.Role.PARTICIPANT,
        )
        self.jury = User.objects.create_user(
            username='jury', password='test-password', role=User.Role.JURY,
            language='fr',
        )
        self.hackathon = make_hackathon(self.organizer, 'CodeStorm One')
        self.foreign_hackathon = make_hackathon(
            self.other_organizer, 'CodeStorm Two',
            start_date=timezone.now() + timedelta(days=1),
        )

    def test_participant_cards_follow_the_rows(self):
        team = make_team(self.hackathon, self.participant, 'Alpha team')
        make_project(self.hackathon, self.participant, title='Alpha', team=team)
        context = dashboards.participant_context(self.participant)
        self.assertEqual(card(context, 'Submitted Projects'), 1)
        self.assertEqual(card(context, 'My Teams'), 1)

        other = make_team(self.hackathon, self.teammate, 'Beta team')
        make_project(
            self.hackathon, self.teammate, title='Beta', team=other,
            status=Project.Status.DRAFT,
        )
        after = dashboards.participant_context(self.participant)
        self.assertEqual(card(after, 'Submitted Projects'), 1)
        self.assertEqual(card(after, 'My Teams'), 1)

    def test_participant_sees_nothing_of_another_team(self):
        make_project(
            self.hackathon, self.teammate, title='Foreign-Draft-Should-Stay-Hidden',
            status=Project.Status.DRAFT,
        )
        self.client.force_login(self.participant)
        response = self.client.get('/')
        self.assertNotContains(response, 'Foreign-Draft-Should-Stay-Hidden')

    def test_organizer_scope_is_limited_to_own_hackathons(self):
        make_project(self.hackathon, self.participant, title='Mine')
        make_project(
            self.foreign_hackathon, self.teammate, title='Not-Mine',
            team=Team.objects.create(
                name='Foreign team', hackathon=self.foreign_hackathon,
                created_by=self.teammate, captain=self.teammate, max_members=5,
            ),
        )
        own = dashboards.organizer_context(self.organizer)
        foreign = dashboards.organizer_context(self.other_organizer)
        self.assertEqual(card(own, 'Hackathons you manage'), 1)
        self.assertEqual(card(foreign, 'Hackathons you manage'), 1)
        self.assertEqual(card(own, 'Submitted projects'), 1)
        self.assertEqual([p.title for p in foreign['recent_projects']], ['Not-Mine'])

    def test_jury_todo_drops_when_a_score_is_submitted(self):
        project = make_project(self.hackathon, self.participant, title='To Grade')
        before = dashboards.jury_context(self.jury)
        self.assertEqual(card(before, 'Projects you can score'), 1)
        self.assertEqual(card(before, 'Evaluations to start'), 1)

        Evaluation.objects.create(project=project, jury=self.jury, is_complete=True).save()
        after = dashboards.jury_context(self.jury)
        self.assertEqual(card(after, 'Evaluations to start'), 0)
        self.assertEqual(card(after, 'Submitted evaluations'), 1)

    def test_jury_criteria_rows_carry_the_hackathon_object(self):
        make_project(self.hackathon, self.participant, title='With Criteria')
        EvaluationCriterion.objects.create(
            hackathon=self.hackathon, name='Innovation', weight=Decimal('40.00'),
        )
        rows = dashboards.jury_context(self.jury)['criteria_rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['hackathon'], self.hackathon)
        self.assertEqual(rows[0]['count'], 1)

    def test_average_score_card_stays_empty_without_published_results(self):
        make_project(self.hackathon, self.participant, title='No Result Yet')
        context = dashboards.participant_context(self.participant)
        self.assertEqual(card(context, 'Average Score'), '--')

    def test_cards_count_every_row_not_only_the_displayed_list(self):
        """A counter standing above a short list still reports the full total."""
        total = dashboards.LIST_LIMIT + 2
        for index in range(total):
            team = make_team(self.hackathon, self.participant, f'Bulk {index}')
            make_project(
                self.hackathon, self.participant, title=f'Bulk {index}', team=team,
            )
        context = dashboards.participant_context(self.participant)
        self.assertEqual(card(context, 'Submitted Projects'), total)
        self.assertEqual(len(context['projects']), dashboards.LIST_LIMIT)

    def test_jury_average_covers_every_submitted_evaluation(self):
        criterion = EvaluationCriterion.objects.create(
            hackathon=self.hackathon, name='Impact', weight=Decimal('50.00'),
        )
        evaluations = []
        for index, score in enumerate((10, 10, 10, 10, 10, 0)):
            project = make_project(self.hackathon, self.participant, title=f'Avg {index}')
            evaluation = Evaluation.objects.create(
                project=project, jury=self.jury, is_complete=True,
            )
            EvaluationScore.objects.create(
                evaluation=evaluation, criterion=criterion, score=Decimal(score),
            )
            evaluations.append(evaluation)

        context = dashboards.jury_context(self.jury)
        expected = round(
            sum(evaluation.total_score for evaluation in evaluations) / len(evaluations), 1,
        )
        note = next(
            card['note'] for card in context['stats_cards']
            if card['label'] == 'Submitted evaluations'
        )
        self.assertEqual(len(context['recent_evaluations']), dashboards.LIST_LIMIT)
        self.assertTrue(note.startswith(str(expected)), f'{note!r} does not start with {expected}')


class DashboardRenderTests(TestCase):
    def setUp(self):
        self.organizer = User.objects.create_user(
            username='organizer', password='test-password', role=User.Role.ORGANIZER,
        )
        self.participant = User.objects.create_user(
            username='participant', password='test-password', role=User.Role.PARTICIPANT,
        )
        self.jury = User.objects.create_user(
            username='jury', password='test-password', role=User.Role.JURY,
        )
        self.hackathon = make_hackathon(self.organizer, 'CodeStorm One')
        make_project(self.hackathon, self.participant, title='Alpha')

    def test_every_role_lands_on_its_own_dashboard(self):
        for user, expected in [
            (self.organizer, 'organizer_dashboard.html'),
            (self.participant, 'participant_dashboard.html'),
            (self.jury, 'jury_dashboard.html'),
        ]:
            self.client.force_login(user)
            response = self.client.get('/')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.templates[0].name, expected)

    def test_role_less_account_falls_back_to_the_neutral_dashboard(self):
        staff = User.objects.create_user(
            username='no-role', password='test-password', role='staff',
        )
        self.client.force_login(staff)
        response = self.client.get('/')
        self.assertEqual(response.templates[0].name, 'dashboard.html')

    def test_prototype_literals_are_gone(self):
        self.client.force_login(self.participant)
        response = self.client.get('/')
        for literal in ['128 Participants', '+2 this month', 'NeuralMatch', 'Code Warriors']:
            self.assertNotContains(response, literal)

    def test_anonymous_is_sent_to_login(self):
        for path in ['/', '/notifications/']:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302)
            self.assertIn('/login/', response.headers['Location'])

    def test_dashboard_queries_do_not_grow_with_the_rows(self):
        # one project per team is a real database constraint, so bulk rows mean
        # bulk teams; the query count must stay flat anyway.
        for index in range(6):
            team = make_team(self.hackathon, self.participant, f'Bulk team {index}')
            Project.objects.create(
                hackathon=self.hackathon, team=team, submitted_by=self.participant,
                title=f'Bulk {index}', description='x',
                status=Project.Status.SUBMITTED,
                repo_url='https://github.com/example/repo',
            )
        self.client.force_login(self.participant)
        self.client.get('/')  # warm up any per-process cache
        with CaptureQueriesContext(connection) as captured:
            self.client.get('/')
        self.assertLess(len(captured.captured_queries), 40)


class NotificationFeedTests(TestCase):
    def setUp(self):
        self.organizer = User.objects.create_user(
            username='organizer', password='test-password', role=User.Role.ORGANIZER,
        )
        self.captain = User.objects.create_user(
            username='captain', password='test-password', role=User.Role.PARTICIPANT,
        )
        self.member = User.objects.create_user(
            username='member', password='test-password', role=User.Role.PARTICIPANT,
        )
        self.jury = User.objects.create_user(
            username='jury', password='test-password', role=User.Role.JURY,
        )
        self.hackathon = make_hackathon(self.organizer, 'CodeStorm One')
        self.team = Team.objects.create(
            name='Captained team', hackathon=self.hackathon, created_by=self.captain,
            captain=self.captain, max_members=5,
        )

    def test_invitation_shows_up_and_leaves_once_answered(self):
        TeamInvitation.objects.create(
            team=self.team, inviter=self.captain, invitee=self.member,
        )
        texts = feed_texts(self.member)
        self.assertTrue(any('Captained team' in text for text in texts))
        self.assertIn('Invitations to answer', feed_titles(self.member))

        invitation = TeamInvitation.objects.get(invitee=self.member)
        invitation.status = TeamInvitation.Status.ACCEPTED
        invitation.save()
        self.assertNotIn('Invitations to answer', feed_titles(self.member))

    def test_expired_invitation_is_never_offered_as_actionable(self):
        TeamInvitation.objects.create(
            team=self.team, inviter=self.captain, invitee=self.member,
            expires_at=timezone.now() - timedelta(hours=1),
        )
        self.assertNotIn('Invitations to answer', feed_titles(self.member))

    def test_join_request_reaches_the_captain_not_the_organizer(self):
        JoinRequest.objects.create(team=self.team, user=self.member)
        self.assertIn('Requests waiting for your decision', feed_titles(self.captain))
        self.assertIn('Your pending requests', feed_titles(self.member))
        self.assertNotIn('Requests waiting for your decision', feed_titles(self.organizer))

    def test_results_stay_hidden_until_the_organizer_publishes(self):
        project = make_project(self.hackathon, self.captain, title='Ranked', team=self.team)
        entry = LeaderboardEntry.objects.create(
            hackathon=self.hackathon, project=project, rank=1,
            final_score=Decimal('8.50'),
        )
        self.assertNotIn('Results published for you', feed_titles(self.captain))

        self.hackathon.results_published = True
        self.hackathon.save()
        self.assertIn('Results published for you', feed_titles(self.captain))
        entry.refresh_from_db()
        self.assertIn(f'is rank {entry.rank}', ' '.join(feed_texts(self.captain)))

    def test_channel_preference_filters_the_team_group(self):
        TeamInvitation.objects.create(
            team=self.team, inviter=self.captain, invitee=self.member,
        )
        self.assertEqual(dashboards.notification_groups(self.member)[1]['actionable'], 1)
        self.member.notif_team = False
        self.member.save()
        self.assertNotIn('Invitations to answer', feed_titles(self.member))
        self.assertEqual(dashboards.notification_groups(self.member)[1]['actionable'], 0)

    def test_organizer_is_told_about_submissions_without_a_score(self):
        make_project(self.hackathon, self.captain, title='Awaiting Jury', team=self.team)
        self.assertIn(
            'Submissions still without a submitted score', feed_titles(self.organizer),
        )
        Evaluation.objects.create(project=Project.objects.get(title='Awaiting Jury'),
                                  jury=self.jury, is_complete=True).save()
        self.assertNotIn(
            'Submissions still without a submitted score', feed_titles(self.organizer),
        )

    def test_feed_entries_link_somewhere_real(self):
        JoinRequest.objects.create(team=self.team, user=self.member)
        self.client.force_login(self.captain)
        groups, _ = dashboards.notification_groups(self.captain)
        urls = [item['url'] for group in groups for item in group['items']]
        self.assertTrue(urls)
        for url in urls:
            self.assertNotEqual(self.client.get(url).status_code, 404)

    def test_context_processor_gives_every_page_the_live_feed(self):
        TeamInvitation.objects.create(
            team=self.team, inviter=self.captain, invitee=self.member,
        )
        self.client.force_login(self.member)
        response = self.client.get('/teams/')
        titles = [group['title'] for group in response.context['notification_feed']]
        self.assertIn('Invitations to answer', titles)
        self.assertContains(response, 'Invitations to answer')

    @override_settings(LANGUAGE_CODE='fr')
    def test_feed_uses_the_activated_language(self):
        JoinRequest.objects.create(team=self.team, user=self.member)
        self.member.language = 'fr'
        self.member.save()
        self.client.force_login(self.member)
        response = self.client.get('/notifications/')
        self.assertContains(response, 'Vos demandes en attente')
        self.assertNotContains(response, 'Your pending requests')


class ErrorPageTests(TestCase):
    """The three error pages a visitor can actually land on.

    The test runner sets DEBUG to False, so these exercise the real handlers
    rather than Django's debug page.
    """

    def setUp(self):
        self.organizer = User.objects.create_user(
            username='organizer', password='test-password', role=User.Role.ORGANIZER,
        )
        self.participant = User.objects.create_user(
            username='participant', password='test-password', role=User.Role.PARTICIPANT,
        )

    def test_unknown_address_renders_the_custom_404(self):
        self.client.force_login(self.participant)
        response = self.client.get('/no-such-page-at-all/')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.templates[0].name, '404.html')
        self.assertContains(response, 'We could not find that page', status_code=404)
        self.assertContains(response, '/no-such-page-at-all/', status_code=404)

    def test_role_guard_failure_renders_the_custom_403(self):
        outsider = User.objects.create_user(
            username='outsider', password='test-password', role='staff',
        )
        self.client.force_login(outsider)
        response = self.client.get('/prizes/')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.templates[0].name, '403.html')
        self.assertContains(response, 'This page is not yours to open', status_code=403)

    def test_403_shows_no_server_internals(self):
        outsider = User.objects.create_user(
            username='outsider', password='test-password', role='staff',
        )
        self.client.force_login(outsider)
        response = self.client.get('/prizes/')
        self.assertEqual(response.status_code, 403)
        body = response.content.decode()
        for leak in ('Traceback', 'PermissionDenied', 'DoesNotExist'):
            self.assertNotIn(leak, body)

    def test_500_renders_without_a_request_context(self):
        # django.views.defaults.server_error renders 500.html with no request at
        # all: the template must not depend on the auth or i18n context processors.
        response = server_error(RequestFactory().get('/boom/'))
        self.assertEqual(response.status_code, 500)
        self.assertIn('Something broke on our side', response.content.decode())

    @override_settings(LANGUAGE_CODE='fr')
    def test_error_pages_are_translated(self):
        # UserLanguageMiddleware reads the profile language first, so the probe
        # accounts have to be French for the catalog to be used.
        self.participant.language = 'fr'
        self.participant.save()
        self.client.force_login(self.participant)
        self.assertContains(
            self.client.get('/no-such-page-at-all/'), 'Page introuvable', status_code=404,
        )
        outsider = User.objects.create_user(
            username='outsider', password='test-password', role='staff', language='fr',
        )
        self.client.force_login(outsider)
        self.assertContains(
            self.client.get('/prizes/'), 'Cette page ne vous est pas destin', status_code=403,
        )
        self.assertIn(
            'Un problème est survenu de notre côté',
            server_error(RequestFactory().get('/boom/')).content.decode(),
        )
