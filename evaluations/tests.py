from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
import json

from accounts.models import User
from evaluations.models import (
    Hackathon, Project, Evaluation, EvaluationCriteria, EvaluationScore,
)


class EvaluationDashboardTests(TestCase):
    def setUp(self):
        self.jury = User.objects.create_user(
            username='jury',
            email='jury@example.com',
            password='pass12345',
            first_name='Sara',
            last_name='Jury',
            role=User.Role.JURY,
        )
        self.hackathon = Hackathon.objects.create(
            name='AI Innovation Challenge',
            track='Healthcare',
            deadline=timezone.now() + timedelta(days=3),
        )
        self.project = Project.objects.create(
            hackathon=self.hackathon,
            name='SmartHealth AI',
            team_name='Code Warriors',
        )
        self.client.force_login(self.jury)

    def test_dashboard_shows_live_stats_after_start(self):
        response = self.client.get(reverse('evaluations:home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Start Evaluation')
        self.assertContains(response, 'startEvalModal')
        self.assertEqual(response.context['total_projects'], 1)
        self.assertEqual(response.context['total_evals'], 0)
        self.assertEqual(response.context['queue_count'], 0)

        start = self.client.post(
            reverse('evaluations:start'),
            data=json.dumps({'project_id': self.project.id}),
            content_type='application/json',
        )
        self.assertEqual(start.status_code, 200)
        payload = start.json()
        self.assertTrue(payload['success'])
        self.assertEqual(Evaluation.objects.count(), 1)

        dashboard = self.client.get(reverse('evaluations:home'))
        self.assertEqual(dashboard.context['queue_count'], 1)
        self.assertEqual(dashboard.context['inprogress_evals'], 1)
        self.assertContains(dashboard, 'SmartHealth AI')
        self.assertContains(dashboard, 'AI Innovation Challenge')

    def test_start_requires_hackathon_project_and_updates_completed_stats(self):
        missing = self.client.post(
            reverse('evaluations:start'),
            data=json.dumps({}),
            content_type='application/json',
        )
        self.assertEqual(missing.status_code, 400)

        start = self.client.post(
            reverse('evaluations:start'),
            data=json.dumps({'project_id': self.project.id}),
            content_type='application/json',
        )
        evaluation_id = start.json()['evaluation_id']
        criteria = list(EvaluationCriteria.objects.filter(hackathon__isnull=True))
        self.assertTrue(criteria)

        scores = {str(c.id): 8 for c in criteria}
        submit = self.client.post(
            reverse('evaluations:submit'),
            data=json.dumps({
                'evaluation_id': evaluation_id,
                'action': 'submit',
                'strengths': 'Strong architecture',
                'improvements': 'Add tests',
                'confidential_notes': 'Top 3',
                'scores': scores,
            }),
            content_type='application/json',
        )
        self.assertEqual(submit.status_code, 200)
        self.assertEqual(submit.json()['status'], Evaluation.Status.COMPLETED)

        dashboard = self.client.get(reverse('evaluations:home'))
        self.assertEqual(dashboard.context['completed_evals'], 1)
        self.assertEqual(dashboard.context['avg_score'], 80)
        self.assertContains(dashboard, 'Strong architecture')
        self.assertEqual(dashboard.context['dist']['proficient'], 1)

    def test_catalog_lists_hackathon_and_projects(self):
        response = self.client.get(reverse('evaluations:catalog'))
        self.assertEqual(response.status_code, 200)
        data = response.json()['hackathons']
        self.assertEqual(data[0]['name'], 'AI Innovation Challenge')
        self.assertEqual(data[0]['projects'][0]['name'], 'SmartHealth AI')
