"""Tests du module Évaluations (travail d'Oussama, adapté aux modèles partagés).

Les tests de base d'Oussama (tableau de bord, démarrage/soumission, catalogue)
sont repris ici contre ``hackathons.Hackathon`` / ``projects.Project`` /
``evaluations`` de ``develop``, et complétés par ce que son ébauche ne couvrait
pas : rôles, cloisonnement entre jurés, notes confidentielles, publication des
résultats et coût en requêtes.
"""
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from analyses.models import AIReport
from hackathons.models import Hackathon
from projects.models import Project
from teams.models import Team

from .models import Evaluation, EvaluationComment, EvaluationCriterion, EvaluationScore


def make_hackathon(organizer, title, **overrides):
    now = timezone.now()
    values = {
        'organizer': organizer,
        'title': title,
        'description': 'Projects are judged against the published rubric.',
        'category': Hackathon.Category.AI,
        'event_format': Hackathon.Format.ONLINE,
        'start_date': now + timedelta(days=1),
        'end_date': now + timedelta(days=3),
        'registration_deadline': now,
        'submission_deadline': now + timedelta(days=2),
        'status': Hackathon.Status.PUBLISHED,
    }
    values.update(overrides)
    return Hackathon.objects.create(**values)


def make_project(hackathon, member, title='SmartHealth AI', **overrides):
    team = overrides.pop('team', None)
    if team is None:
        team = Team.objects.create(
            name=f'{title} team', hackathon=hackathon,
            created_by=member, captain=member, max_members=5,
        )
    values = {
        'hackathon': hackathon, 'team': team, 'submitted_by': member,
        'title': title, 'description': 'A judged project.', 'status': Project.Status.SUBMITTED,
        'repo_url': 'https://github.com/example/repo',
    }
    values.update(overrides)
    return Project.objects.create(**values)


class EvaluationBase(TestCase):
    def setUp(self):
        self.organizer = User.objects.create_user(
            username='organizer', password='test-password', role=User.Role.ORGANIZER,
        )
        self.participant = User.objects.create_user(
            username='participant', password='test-password', role=User.Role.PARTICIPANT,
        )
        self.jury = User.objects.create_user(
            username='jury', password='test-password', role=User.Role.JURY,
            first_name='Sara', last_name='Jury',
        )
        self.other_jury = User.objects.create_user(
            username='other-jury', password='test-password', role=User.Role.JURY,
        )
        self.captain = User.objects.create_user(
            username='captain', password='test-password', role=User.Role.PARTICIPANT,
        )
        self.hackathon = make_hackathon(self.organizer, 'AI Innovation Challenge')
        self.project = make_project(self.hackathon, self.captain)

    def login(self, user):
        self.client.force_login(user)

    def start(self, project=None):
        return self.client.post(
            reverse('evaluations:start'),
            data={'project_id': (project or self.project).pk},
            content_type='application/json',
        )

    def rubric(self):
        return list(EvaluationCriterion.objects.filter(hackathon=self.hackathon))


class EvaluationDashboardTests(EvaluationBase):
    """Les fonctionnalités d'Oussama, sur les modèles de develop."""

    def test_dashboard_shows_live_stats_after_start(self):
        self.login(self.jury)
        response = self.client.get(reverse('evaluations:home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'startEvalModal')
        self.assertContains(response, '+ Start Evaluation')
        self.assertEqual(response.context['total_projects'], 1)
        self.assertEqual(response.context['total_evals'], 0)
        self.assertEqual(response.context['queue_count'], 0)

        start = self.start()
        self.assertEqual(start.status_code, 200)
        payload = start.json()
        self.assertTrue(payload['success'])
        self.assertEqual(Evaluation.objects.count(), 1)

        dashboard = self.client.get(reverse('evaluations:home'))
        self.assertEqual(dashboard.context['queue_count'], 1)
        self.assertEqual(dashboard.context['inprogress_evals'], 1)
        self.assertContains(dashboard, self.project.title)
        self.assertContains(dashboard, self.hackathon.title)
        self.assertContains(dashboard, 'Continue Review')

    def test_start_and_submit_update_the_completed_stats(self):
        self.login(self.jury)
        evaluation_id = self.start().json()['evaluation_id']
        criteria = self.rubric()
        self.assertTrue(criteria)

        submit = self.client.post(
            reverse('evaluations:submit'),
            data={
                'evaluation_id': evaluation_id,
                'action': 'submit',
                'strengths': 'Strong architecture',
                'improvements': 'Add tests',
                'confidential_notes': 'Top 3',
                'scores': {str(c.pk): 8 for c in criteria},
            },
            content_type='application/json',
        )
        self.assertEqual(submit.status_code, 200)
        self.assertEqual(submit.json()['status'], Evaluation.Status.COMPLETED)

        evaluation = Evaluation.objects.get(pk=evaluation_id)
        self.assertTrue(evaluation.is_complete)

        dashboard = self.client.get(reverse('evaluations:home'))
        self.assertEqual(dashboard.context['completed_evals'], 1)
        self.assertEqual(dashboard.context['avg_score'], 80)
        self.assertContains(dashboard, 'Strong architecture')
        self.assertEqual(dashboard.context['dist']['proficient'], 1)

    def test_catalog_lists_hackathon_and_evaluable_projects(self):
        self.login(self.jury)
        response = self.client.get(reverse('evaluations:catalog'))
        self.assertEqual(response.status_code, 200)
        data = response.json()['hackathons']
        self.assertEqual(data[0]['name'], 'AI Innovation Challenge')
        self.assertEqual(data[0]['track'], 'AI & Machine Learning')
        self.assertEqual(data[0]['projects'][0]['name'], self.project.title)
        self.assertEqual(data[0]['projects'][0]['team_name'], self.project.team.name)

    def test_catalog_hides_draft_projects(self):
        make_project(self.hackathon, self.captain, title='Private Draft',
                     status=Project.Status.DRAFT)
        self.login(self.jury)
        data = self.client.get(reverse('evaluations:catalog')).json()['hackathons']
        names = [p['name'] for h in data for p in h['projects']]
        self.assertNotIn('Private Draft', names)

    def test_rubric_is_created_for_the_hackathon_not_globally(self):
        """Le jeu de critères par défaut doit rester rattaché au hackathon.

        ``prizes/services.py`` refuse toute note dont le critère n'appartient pas
        au hackathon classé : une rubrique globale casserait les Prix.
        """
        self.login(self.jury)
        self.assertEqual(EvaluationCriterion.objects.count(), 0)
        self.start()
        criteria = self.rubric()
        self.assertEqual(len(criteria), 5)
        self.assertTrue(all(c.hackathon_id == self.hackathon.pk for c in criteria))

    def test_total_score_is_the_weighted_score_rescaled(self):
        """Une seule formule d'agrégat pour le dashboard, le classement et les Prix."""
        self.login(self.jury)
        evaluation_id = self.start().json()['evaluation_id']
        evaluation = Evaluation.objects.get(pk=evaluation_id)
        criterion = EvaluationCriterion.objects.create(
            hackathon=self.hackathon, name='Innovation', weight=Decimal('40.00'),
        )
        EvaluationScore.objects.create(
            evaluation=evaluation, criterion=criterion, score=Decimal('9.00'),
        )
        evaluation.refresh_from_db()
        self.assertEqual(evaluation.get_weighted_score(), 9.0)
        self.assertEqual(evaluation.total_score, 90)

    def test_completing_an_evaluation_also_completes_its_status(self):
        self.login(self.jury)
        evaluation = Evaluation.objects.create(
            project=self.project, jury=self.jury, is_complete=True,
        )
        evaluation.save()
        evaluation.refresh_from_db()
        self.assertEqual(evaluation.status, Evaluation.Status.COMPLETED)


class EvaluationAccessTests(EvaluationBase):
    """Un participant ne doit rien voir du travail du jury."""

    def test_participant_is_refused_everywhere(self):
        self.login(self.participant)
        for name in ('home', 'catalog', 'results', 'export'):
            with self.subTest(view=name):
                response = self.client.get(reverse(f'evaluations:{name}'))
                self.assertEqual(response.status_code, 403)
        for name, kwargs in (
            ('start', {}), ('submit', {}), ('add_comment', {'evaluation_id': 1}),
        ):
            with self.subTest(view=name):
                response = self.client.post(reverse(f'evaluations:{name}', kwargs=kwargs),
                                            data='{}', content_type='application/json')
                self.assertEqual(response.status_code, 403)
        response = self.client.get(
            reverse('evaluations:detail', kwargs={'project_id': self.project.pk}))
        self.assertEqual(response.status_code, 403)

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(reverse('evaluations:home'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])

    def test_sidebar_only_offers_evaluations_to_those_who_may_open_it(self):
        """Le lien de navigation ne doit pas conduire à un 403."""
        self.login(self.participant)
        self.assertNotIn('data-path="evaluations"',
                         self.client.get(reverse('dashboard')).content.decode())
        self.login(self.jury)
        self.assertIn('data-path="evaluations"',
                      self.client.get(reverse('dashboard')).content.decode())

    def test_platform_admin_reads_the_workspace_but_never_scores(self):
        """Un superutilisateur administre, il ne se fait pas passer pour un jury."""
        admin = User.objects.create_user(
            username='platform-admin', password='test-password',
            role=User.Role.PARTICIPANT,
        )
        admin.is_staff = True
        admin.is_superuser = True
        admin.save(update_fields=['is_staff', 'is_superuser'])
        self.login(self.jury)
        self.start()
        self.login(admin)
        self.assertEqual(self.client.get(reverse('evaluations:home')).status_code, 200)
        self.assertEqual(self.client.get(reverse('evaluations:results')).status_code, 200)
        self.assertEqual(self.start().status_code, 403)
        self.assertIn('Notes', self.client.get(
            reverse('evaluations:export')).content.decode().splitlines()[0])

    def test_organizer_reads_but_never_scores(self):
        self.login(self.jury)
        self.start()
        self.login(self.organizer)
        self.assertEqual(self.client.get(reverse('evaluations:home')).status_code, 200)
        self.assertEqual(self.client.get(reverse('evaluations:results')).status_code, 200)
        self.assertEqual(self.start().status_code, 403)
        self.assertEqual(
            self.client.get(reverse('evaluations:detail',
                                    kwargs={'project_id': self.project.pk})).status_code,
            403,
        )

    def test_organizer_dashboard_hides_the_jury_actions(self):
        """La fiche d'un projet en cours reste visible, sans bouton de notation."""
        self.login(self.jury)
        self.start()
        self.login(self.organizer)
        content = self.client.get(reverse('evaluations:home')).content.decode()
        self.assertIn(self.project.title, content)
        self.assertNotIn('+ Start Evaluation', content)
        self.assertNotIn('Continue Review', content)
        self.assertNotIn('Review Project', content)

    def test_a_jury_cannot_open_another_jury_draft(self):
        self.login(self.jury)
        self.start()
        self.login(self.other_jury)
        response = self.client.get(
            reverse('evaluations:detail', kwargs={'project_id': self.project.pk}))
        self.assertEqual(response.status_code, 404)

    def test_a_jury_cannot_submit_for_another_jury(self):
        self.login(self.jury)
        evaluation_id = self.start().json()['evaluation_id']
        self.login(self.other_jury)
        response = self.client.post(
            reverse('evaluations:submit'),
            data={'evaluation_id': evaluation_id, 'action': 'submit',
                  'scores': {'1': 10}},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 404)
        evaluation = Evaluation.objects.get(pk=evaluation_id)
        self.assertNotEqual(evaluation.status, Evaluation.Status.COMPLETED)

    def test_dashboard_of_a_jury_never_shows_a_peers_evaluation(self):
        self.login(self.jury)
        evaluation_id = self.start().json()['evaluation_id']
        Evaluation.objects.filter(pk=evaluation_id).update(
            strengths='Secret peer analysis', is_complete=True,
            status=Evaluation.Status.COMPLETED,
        )
        self.login(self.other_jury)
        body = self.client.get(reverse('evaluations:home')).content.decode()
        self.assertNotIn('Secret peer analysis', body)

    def test_confidential_notes_stay_out_of_the_dashboard_and_the_csv(self):
        self.login(self.jury)
        evaluation_id = self.start().json()['evaluation_id']
        Evaluation.objects.filter(pk=evaluation_id).update(
            confidential_notes='DO-NOT-SHARE-SECRET', is_complete=True,
            status=Evaluation.Status.COMPLETED,
        )
        body = self.client.get(reverse('evaluations:home')).content.decode()
        self.assertNotIn('DO-NOT-SHARE-SECRET', body)

        csv_body = self.client.get(reverse('evaluations:export')).content.decode()
        self.assertNotIn('DO-NOT-SHARE-SECRET', csv_body)
        self.assertNotIn('Notes', csv_body.splitlines()[0])

        self.login(self.organizer)
        organizer_csv = self.client.get(reverse('evaluations:export')).content.decode()
        self.assertIn('Notes', organizer_csv.splitlines()[0])
        self.assertIn('DO-NOT-SHARE-SECRET', organizer_csv)


class EvaluationPublicationTests(EvaluationBase):
    """Le classement ne présente que les hackathons dont les résultats sont publiés."""

    def _completed(self, title, score=9, published=False):
        hackathon = make_hackathon(self.organizer, title, results_published=published)
        project = make_project(hackathon, self.captain, title=f'{title} project')
        evaluation = Evaluation.objects.create(
            project=project, jury=self.jury, is_complete=True,
            status=Evaluation.Status.COMPLETED,
        )
        criterion = EvaluationCriterion.objects.create(
            hackathon=hackathon, name='Innovation', weight=Decimal('50.00'),
        )
        EvaluationScore.objects.create(
            evaluation=evaluation, criterion=criterion, score=Decimal(f'{score}.00'),
        )
        return hackathon, evaluation

    def test_jury_sees_only_published_results(self):
        published, published_eval = self._completed('Published Cup', published=True)
        hidden, hidden_eval = self._completed('Pending Cup')

        self.login(self.jury)
        response = self.client.get(reverse('evaluations:results'))
        self.assertContains(response, published_eval.project.title)
        self.assertNotContains(response, hidden_eval.project.title)
        self.assertNotIn(hidden.pk, [h.pk for h in response.context['hackathons']])

    def test_guessed_hackathon_id_cannot_bypass_publication(self):
        _, hidden_eval = self._completed('Pending Cup')
        self.login(self.jury)
        response = self.client.get(
            reverse('evaluations:results') + f'?hackathon={hidden_eval.project.hackathon_id}')
        self.assertNotContains(response, hidden_eval.project.title)

    def test_organizer_sees_results_before_publishing(self):
        _, hidden_eval = self._completed('Pending Cup')
        self.login(self.organizer)
        self.assertContains(
            self.client.get(reverse('evaluations:results')), hidden_eval.project.title)

    def test_ranking_is_deterministic(self):
        hackathon = make_hackathon(self.organizer, 'Tied Cup', results_published=True)
        for title, score in (('Tie A', 8), ('Tie B', 8), ('Tie C', 9)):
            project = make_project(hackathon, self.captain, title=title)
            evaluation = Evaluation.objects.create(
                project=project, jury=self.jury, is_complete=True,
                status=Evaluation.Status.COMPLETED,
            )
            criterion = EvaluationCriterion.objects.create(
                hackathon=hackathon, name='Innovation', weight=Decimal('50.00'),
            )
            EvaluationScore.objects.create(
                evaluation=evaluation, criterion=criterion, score=Decimal(f'{score}.00'),
            )
        self.login(self.jury)
        url = reverse('evaluations:results') + f'?hackathon={hackathon.pk}'
        first = [e.project.title for e in self.client.get(url).context['ranked']]
        second = [e.project.title for e in self.client.get(url).context['ranked']]
        self.assertEqual(first, second)
        self.assertEqual(first[0], 'Tie C')


class EvaluationCommentTests(EvaluationBase):
    def test_jury_can_comment_on_its_own_evaluation(self):
        self.login(self.jury)
        evaluation_id = self.start().json()['evaluation_id']
        response = self.client.post(
            reverse('evaluations:add_comment', kwargs={'evaluation_id': evaluation_id}),
            data={'text': 'Can you confirm the demo link?'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(EvaluationComment.objects.count(), 1)

    def test_organizer_can_comment_on_a_jury_evaluation(self):
        self.login(self.jury)
        evaluation_id = self.start().json()['evaluation_id']
        self.login(self.organizer)
        response = self.client.post(
            reverse('evaluations:add_comment', kwargs={'evaluation_id': evaluation_id}),
            data={'text': 'Please finish before the deadline.'},
        )
        self.assertEqual(response.status_code, 200)

    def test_other_jury_is_refused_and_empty_comment_is_rejected(self):
        self.login(self.jury)
        evaluation_id = self.start().json()['evaluation_id']
        self.login(self.other_jury)
        response = self.client.post(
            reverse('evaluations:add_comment', kwargs={'evaluation_id': evaluation_id}),
            data={'text': 'not mine'},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(EvaluationComment.objects.count(), 0)

        self.login(self.jury)
        empty = self.client.post(
            reverse('evaluations:add_comment', kwargs={'evaluation_id': evaluation_id}),
            data={'text': '   '},
        )
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(EvaluationComment.objects.count(), 0)


class EvaluationValidationTests(EvaluationBase):
    def test_invalid_json_is_a_400_not_a_500(self):
        self.login(self.jury)
        response = self.client.post(
            reverse('evaluations:start'), data='{oops', content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_start_without_project_is_rejected(self):
        self.login(self.jury)
        response = self.client.post(
            reverse('evaluations:start'), data={}, content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Evaluation.objects.count(), 0)

    def test_start_on_a_draft_project_is_a_404(self):
        draft = make_project(self.hackathon, self.captain, title='Draft only',
                             status=Project.Status.DRAFT)
        self.login(self.jury)
        self.assertEqual(self.start(draft).status_code, 404)

    def test_scores_of_another_hackathon_are_ignored(self):
        other = make_hackathon(self.organizer, 'Other Cup')
        foreign = EvaluationCriterion.objects.create(
            hackathon=other, name='Foreign criterion', weight=Decimal('50.00'),
        )
        self.login(self.jury)
        evaluation_id = self.start().json()['evaluation_id']
        self.client.post(
            reverse('evaluations:submit'),
            data={'evaluation_id': evaluation_id, 'action': 'submit',
                  'scores': {str(foreign.pk): 10}},
            content_type='application/json',
        )
        self.assertEqual(
            EvaluationScore.objects.filter(evaluation_id=evaluation_id).count(), 0)

    def test_out_of_range_scores_are_clamped(self):
        self.login(self.jury)
        evaluation_id = self.start().json()['evaluation_id']
        criterion = self.rubric()[0]
        self.client.post(
            reverse('evaluations:submit'),
            data={'evaluation_id': evaluation_id, 'action': 'submit',
                  'scores': {str(criterion.pk): 99}},
            content_type='application/json',
        )
        score = EvaluationScore.objects.get(evaluation_id=evaluation_id, criterion=criterion)
        self.assertEqual(score.score, Decimal('10'))


class EvaluationQueryCostTests(EvaluationBase):
    """Le tableau de bord ne doit pas payer une requête par évaluation."""

    def setUp(self):
        super().setUp()
        EvaluationCriterion.objects.create(
            hackathon=self.hackathon, name='Innovation', weight=Decimal('50.00'),
        )

    def _fill(self, count):
        for index in range(count):
            team = Team.objects.create(
                name=f'Team {index}', hackathon=self.hackathon,
                created_by=self.captain, captain=self.captain, max_members=5,
            )
            project = make_project(
                self.hackathon, self.captain, title=f'Project {index}', team=team,
            )
            evaluation = Evaluation.objects.create(
                project=project, jury=self.jury, is_complete=True,
                status=Evaluation.Status.COMPLETED, strengths=f'Note {index}',
            )
            for criterion in EvaluationCriterion.objects.filter(hackathon=self.hackathon):
                EvaluationScore.objects.create(
                    evaluation=evaluation, criterion=criterion, score=Decimal('8.00'),
                )

    def _query_count(self):
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse('evaluations:home'))
        return len(ctx), response

    def test_home_query_count_does_not_grow_with_evaluations(self):
        self.login(self.jury)
        self._fill(2)
        few, resp_few = self._query_count()
        self.assertEqual(resp_few.context['total_evals'], 2)

        self._fill(8)
        many, resp_many = self._query_count()
        self.assertEqual(resp_many.context['total_evals'], 10)
        self.assertEqual(few, many)


class EvaluationFrenchInterfaceTests(EvaluationBase):
    """Les pages intégrées doivent se traduire avec le catalogue FR."""

    def setUp(self):
        super().setUp()
        self.jury.language = 'fr'
        self.jury.save(update_fields=['language'])

    def _published_evaluation(self):
        hackathon = make_hackathon(self.organizer, 'Published Cup', results_published=True)
        project = make_project(hackathon, self.captain, title='Published Cup project')
        evaluation = Evaluation.objects.create(
            project=project, jury=self.jury, is_complete=True,
            status=Evaluation.Status.COMPLETED, strengths='Architecture solide',
        )
        for name in ('Innovation', 'Impact'):
            criterion = EvaluationCriterion.objects.create(
                hackathon=hackathon, name=name, weight=Decimal('50.00'),
            )
            EvaluationScore.objects.create(
                evaluation=evaluation, criterion=criterion, score=Decimal('8.00'),
            )
        return hackathon, evaluation

    def test_dashboard_renders_in_french(self):
        self._published_evaluation()
        self.login(self.jury)
        content = self.client.get(reverse('evaluations:home')).content.decode()
        for expected in (
            'Démarrer l’évaluation',              # {% trans %}
            'Répartition sur',                    # {% blocktrans count %}
            'sont entièrement notées',            # {% blocktrans %}
            'il y a',                             # {% blocktrans with %}
        ):
            self.assertIn(expected, content)

    def test_results_page_renders_in_french(self):
        self._published_evaluation()
        self.login(self.jury)
        content = self.client.get(reverse('evaluations:results')).content.decode()
        for expected in (
            'Classement des résultats',
            'CODESTORM :: CLASSEMENT FINAL',
            'Projet et équipe',
            'Points forts',
        ):
            self.assertIn(expected, content)


class ScoringAssistantBridgeTests(EvaluationBase):
    """Passerelle avec le module IA : une suggestion se consulte, elle ne s'écrit jamais seule.

    Le rapport d'analyse est un objet d'aide à la décision : la fiche d'évaluation
    du jury reste la seule source d'une note, et un simple GET ne doit rien écrire.
    """

    def make_report(self, status=AIReport.Status.DONE, score=7):
        entries = [{
            'key': f'c{c.pk}', 'label': c.name, 'criterion_id': c.pk,
            'weight': float(c.weight), 'score': score,
            'rationale': 'Aucun test automatique dans le dépôt.',
            'evidence': ['app.py'],
        } for c in self.rubric()]
        return AIReport.objects.create(
            created_by=self.organizer, project=self.project,
            repository_url=self.project.repo_url, status=status,
            model_id='test/model', file_count=3,
            report={
                'summary': 'Un dépôt Python avec une README.',
                'technologies': ['Python'], 'strengths': ['Structure claire'],
                'concerns': ['Aucun test'], 'limitations': 'Uniquement les fichiers lus.',
                'criteria': entries, 'scale': {'min': 0, 'max': 10},
                'unverified_evidence': 0, 'rubric_source': 'hackathon',
                'coverage': {'read_files': ['app.py'], 'truncated_files': [],
                             'ignored_files': []},
            },
        )

    def open_form(self):
        """Le juré démarre son évaluation, puis charge le JSON du formulaire."""
        self.login(self.jury)
        self.start()          # ensure_rubric() : les critères du jury existent ici
        return self.client.get(reverse('evaluations:detail', args=[self.project.pk]))

    def test_detail_exposes_suggestions_without_writing_any_score(self):
        self.login(self.jury)
        self.start()
        report = self.make_report()
        analysis = self.client.get(
            reverse('evaluations:detail', args=[self.project.pk])).json()['analysis']
        self.assertEqual(analysis['status'], 'done')
        self.assertEqual(analysis['scale_max'], 10)
        self.assertIn(f'/analyses/{report.pk}/', analysis['report_url'])
        self.assertEqual(
            sorted(entry['criterion_id'] for entry in analysis['criteria']),
            sorted(c.pk for c in self.rubric()))
        # La suggestion reste une suggestion : aucune note n'est écrite.
        self.assertEqual(EvaluationScore.objects.count(), 0)
        self.assertEqual(Evaluation.objects.get().total_score, 0)

    def test_absent_analysis_stays_absent(self):
        analysis = self.open_form().json()['analysis']
        self.assertIsNone(analysis)
        self.assertEqual(AIReport.objects.count(), 0)

    def test_running_analysis_is_reported_as_running(self):
        self.login(self.jury)
        self.start()
        self.make_report(status=AIReport.Status.RUNNING)
        analysis = self.client.get(
            reverse('evaluations:detail', args=[self.project.pk])).json()['analysis']
        self.assertEqual(analysis['status'], 'running')
        self.assertNotIn('criteria', analysis)

    def test_failed_analysis_explains_itself(self):
        self.login(self.jury)
        self.start()
        report = self.make_report(status=AIReport.Status.FAILED)
        report.report = {}
        report.error_message = 'The repository was not found. Make sure it is public.'
        report.save()
        analysis = self.client.get(
            reverse('evaluations:detail', args=[self.project.pk])).json()['analysis']
        self.assertEqual(analysis['status'], 'failed')
        self.assertIn('not found', analysis['error_message'])

    def test_generic_report_never_suggests_a_jury_criterion_score(self):
        # Une revue technique sans critères de l'événement ne doit proposer de
        # note pour aucun critère du jury : rien n'est rattaché, rien n'est repris.
        self.login(self.jury)
        self.start()
        report = self.make_report()
        report.report = dict(report.report, **{
            'scale': {'min': 0, 'max': 5},
            'criteria': [{'key': 'relevance', 'label': 'Relevance',
                          'criterion_id': None, 'weight': None, 'score': 3,
                          'rationale': 'The repository matches the brief.',
                          'evidence': ['README.md']}],
        })
        report.save()
        analysis = self.client.get(
            reverse('evaluations:detail', args=[self.project.pk])).json()['analysis']
        self.assertEqual(analysis['scale_max'], 5)
        self.assertIsNone(analysis['criteria'][0]['criterion_id'])
        self.assertEqual(EvaluationScore.objects.count(), 0)
