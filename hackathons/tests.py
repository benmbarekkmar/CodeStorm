import json
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from . import ai
from .models import Challenge, Hackathon

PASSWORD = 'Str0ng-pass!'


def make_user(username, role):
    return User.objects.create_user(username=username, password=PASSWORD, role=role)


def make_hackathon(organizer, **kwargs):
    now = timezone.now()
    data = dict(
        organizer=organizer, title='AI Challenge', description='Build AI things',
        category=Hackathon.Category.AI,
        start_date=now + timedelta(days=10), end_date=now + timedelta(days=12),
        registration_deadline=now + timedelta(days=5), submission_deadline=now + timedelta(days=11),
        status=Hackathon.Status.PUBLISHED,
    )
    data.update(kwargs)
    return Hackathon.objects.create(**data)


def form_data(**overrides):
    now = timezone.now()
    fmt = '%Y-%m-%dT%H:%M'
    data = {
        'title': 'New Hackathon', 'category': 'web', 'description': 'Great event',
        'event_format': 'online', 'location': '',
        'start_date': (now + timedelta(days=10)).strftime(fmt),
        'end_date': (now + timedelta(days=12)).strftime(fmt),
        'registration_deadline': (now + timedelta(days=5)).strftime(fmt),
        'submission_deadline': (now + timedelta(days=11)).strftime(fmt),
        'max_participants': 100, 'min_team_size': 2, 'max_team_size': 5,
    }
    data.update(overrides)
    return data


class HackathonModelTests(TestCase):
    def setUp(self):
        self.org = make_user('org', 'organizer')

    def test_end_before_start_is_invalid(self):
        h = make_hackathon(self.org)
        h.end_date = h.start_date - timedelta(hours=1)
        with self.assertRaises(ValidationError) as ctx:
            h.full_clean()
        self.assertIn('end_date', ctx.exception.message_dict)

    def test_registration_after_start_is_invalid(self):
        h = make_hackathon(self.org)
        h.registration_deadline = h.start_date + timedelta(days=1)
        with self.assertRaises(ValidationError):
            h.full_clean()

    def test_submission_outside_event_is_invalid(self):
        h = make_hackathon(self.org)
        h.submission_deadline = h.end_date + timedelta(days=1)
        with self.assertRaises(ValidationError):
            h.full_clean()

    def test_min_team_greater_than_max_is_invalid(self):
        h = make_hackathon(self.org, min_team_size=6, max_team_size=3)
        with self.assertRaises(ValidationError):
            h.full_clean()

    def test_in_person_requires_location(self):
        h = make_hackathon(self.org, event_format='in_person', location='')
        with self.assertRaises(ValidationError):
            h.full_clean()

    def test_phase_computation(self):
        now = timezone.now()
        self.assertEqual(make_hackathon(self.org).phase, 'upcoming')
        ongoing = make_hackathon(self.org, start_date=now - timedelta(days=1),
                                 end_date=now + timedelta(days=1),
                                 registration_deadline=now - timedelta(days=2),
                                 submission_deadline=now + timedelta(hours=5))
        self.assertEqual(ongoing.phase, 'ongoing')
        self.assertEqual(make_hackathon(self.org, status='draft').phase, 'draft')
        self.assertEqual(make_hackathon(self.org, status='cancelled').phase, 'cancelled')

    def test_challenge_deadline_must_be_within_event(self):
        h = make_hackathon(self.org)
        c = Challenge(hackathon=h, title='C', description='d', deadline=h.end_date + timedelta(days=3))
        with self.assertRaises(ValidationError):
            c.full_clean()


class HackathonViewTests(TestCase):
    def setUp(self):
        self.org = make_user('org', 'organizer')
        self.other_org = make_user('org2', 'organizer')
        self.participant = make_user('part', 'participant')
        self.hackathon = make_hackathon(self.org)

    def login(self, user):
        self.client.login(username=user.username, password=PASSWORD)

    # --- accès / permissions
    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(reverse('hackathons:home'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response['Location'])

    def test_participant_can_list_and_view_published(self):
        self.login(self.participant)
        self.assertEqual(self.client.get(reverse('hackathons:home')).status_code, 200)
        self.assertEqual(self.client.get(reverse('hackathons:detail', args=[self.hackathon.pk])).status_code, 200)

    def test_participant_cannot_create_update_or_delete(self):
        self.login(self.participant)
        self.assertEqual(self.client.get(reverse('hackathons:create')).status_code, 403)
        self.assertEqual(self.client.post(reverse('hackathons:create'), form_data()).status_code, 403)
        self.assertEqual(self.client.get(reverse('hackathons:update', args=[self.hackathon.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse('hackathons:delete', args=[self.hackathon.pk])).status_code, 403)
        self.assertTrue(Hackathon.objects.filter(pk=self.hackathon.pk).exists())

    def test_other_organizer_cannot_edit_or_delete(self):
        self.login(self.other_org)
        self.assertEqual(self.client.get(reverse('hackathons:update', args=[self.hackathon.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('hackathons:delete', args=[self.hackathon.pk])).status_code, 404)
        self.assertTrue(Hackathon.objects.filter(pk=self.hackathon.pk).exists())

    def test_draft_is_hidden_from_others(self):
        draft = make_hackathon(self.org, status='draft', title='Secret')
        self.login(self.participant)
        self.assertEqual(self.client.get(reverse('hackathons:detail', args=[draft.pk])).status_code, 404)
        self.login(self.org)
        self.assertEqual(self.client.get(reverse('hackathons:detail', args=[draft.pk])).status_code, 200)

    # --- CRUD
    def test_organizer_creates_hackathon_as_draft(self):
        self.login(self.org)
        response = self.client.post(reverse('hackathons:create'), form_data(title='Created'))
        created = Hackathon.objects.get(title='Created')
        self.assertRedirects(response, reverse('hackathons:detail', args=[created.pk]))
        self.assertEqual(created.organizer, self.org)
        self.assertEqual(created.status, 'draft')

    def test_invalid_dates_are_rejected_by_server(self):
        self.login(self.org)
        bad = form_data(title='Bad', end_date=(timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M'))
        response = self.client.post(reverse('hackathons:create'), bad)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Hackathon.objects.filter(title='Bad').exists())

    def test_organizer_updates_and_deletes(self):
        self.login(self.org)
        self.client.post(reverse('hackathons:update', args=[self.hackathon.pk]), form_data(title='Renamed'))
        self.hackathon.refresh_from_db()
        self.assertEqual(self.hackathon.title, 'Renamed')
        self.client.post(reverse('hackathons:delete', args=[self.hackathon.pk]))
        self.assertFalse(Hackathon.objects.filter(pk=self.hackathon.pk).exists())

    def test_publish_toggle_and_cancel(self):
        draft = make_hackathon(self.org, status='draft')
        self.login(self.org)
        self.client.post(reverse('hackathons:toggle_publish', args=[draft.pk]))
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'published')
        self.client.post(reverse('hackathons:cancel', args=[draft.pk]))
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'cancelled')

    def test_publish_requires_post(self):
        self.login(self.org)
        self.assertEqual(self.client.get(reverse('hackathons:toggle_publish', args=[self.hackathon.pk])).status_code, 405)

    # --- challenges
    def test_challenge_lifecycle_and_visibility(self):
        self.login(self.org)
        self.client.post(reverse('hackathons:challenge_create', args=[self.hackathon.pk]),
                         {'title': 'Chall 1', 'description': 'Do it', 'difficulty': 'easy'})
        challenge = Challenge.objects.get(title='Chall 1')
        self.assertFalse(challenge.is_published)

        self.login(self.participant)
        page = self.client.get(reverse('hackathons:detail', args=[self.hackathon.pk]))
        self.assertNotContains(page, 'Chall 1')

        self.login(self.org)
        self.client.post(reverse('hackathons:challenge_toggle_publish', args=[challenge.pk]))
        self.login(self.participant)
        page = self.client.get(reverse('hackathons:detail', args=[self.hackathon.pk]))
        self.assertContains(page, 'Chall 1')

    def test_other_organizer_cannot_touch_challenge(self):
        challenge = Challenge.objects.create(hackathon=self.hackathon, title='C', description='d')
        self.login(self.other_org)
        self.assertEqual(self.client.post(reverse('hackathons:challenge_delete', args=[challenge.pk])).status_code, 404)


class AIEndpointTests(TestCase):
    def setUp(self):
        self.org = make_user('org', 'organizer')
        self.participant = make_user('part', 'participant')
        self.url = reverse('hackathons:generate_description')

    def post(self, payload):
        return self.client.post(self.url, json.dumps(payload), content_type='application/json')

    def test_participant_forbidden(self):
        self.client.login(username='part', password=PASSWORD)
        self.assertEqual(self.post({'title': 'Movie night'}).status_code, 403)

    def test_get_not_allowed(self):
        self.client.login(username='org', password=PASSWORD)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_short_title_rejected(self):
        self.client.login(username='org', password=PASSWORD)
        self.assertEqual(self.post({'title': 'a'}).status_code, 400)

    @patch('hackathons.views.ai.generate_description', return_value='A great event.')
    def test_success(self, mocked):
        self.client.login(username='org', password=PASSWORD)
        response = self.post({'title': 'Movie night'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['description'], 'A great event.')
        self.assertIn('elapsed_ms', response.json())

    @patch('hackathons.views.ai.generate_description', side_effect=ai.AIServiceError('down'))
    def test_ai_failure_returns_502(self, mocked):
        self.client.login(username='org', password=PASSWORD)
        self.assertEqual(self.post({'title': 'Movie night'}).status_code, 502)