from datetime import timedelta
from decimal import Decimal
from io import BytesIO
import tempfile

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from accounts.models import User
from evaluations.models import Evaluation, EvaluationCriterion, EvaluationScore
from hackathons.models import Hackathon
from prizes.models import LeaderboardEntry
from projects.forms import MAX_BANNER_BYTES, _reject_oversized
from projects.models import Project, normalize_technologies
from teams.models import Team, TeamMembership


def make_hackathon(organizer, title, **when):
    now = timezone.now()

    def moment(name, default):
        return now + when.get(name, default)

    return Hackathon.objects.create(
        organizer=organizer,
        title=title,
        description='A hackathon used by the project tests.',
        category=Hackathon.Category.WEB,
        event_format=Hackathon.Format.ONLINE,
        start_date=moment('start', timedelta(hours=-2)),
        end_date=moment('end', timedelta(days=3)),
        registration_deadline=moment('registration', timedelta(days=-1)),
        submission_deadline=moment('submission', timedelta(days=2)),
        status=Hackathon.Status.PUBLISHED,
    )


def make_team(name, hackathon, user):
    team = Team.objects.create(
        name=name, hackathon=hackathon, created_by=user, captain=user, max_members=5,
    )
    team.add_member(user, role=TeamMembership.Role.CAPTAIN)
    return team


class ProjectWorkflowTests(TestCase):
    def setUp(self):
        self.organizer = User.objects.create_user(
            username='organizer', password='test-password', role=User.Role.ORGANIZER,
        )
        self.other_organizer = User.objects.create_user(
            username='other-organizer', password='test-password', role=User.Role.ORGANIZER,
        )
        self.captain = User.objects.create_user(
            username='captain', password='test-password', role=User.Role.PARTICIPANT,
        )
        self.member = User.objects.create_user(
            username='member', password='test-password', role=User.Role.PARTICIPANT,
        )
        self.other = User.objects.create_user(
            username='other', password='test-password', role=User.Role.PARTICIPANT,
        )
        self.jury = User.objects.create_user(
            username='jury', password='test-password', role=User.Role.JURY,
        )
        self.hackathon = make_hackathon(self.organizer, 'Open Cup')
        self.side = make_hackathon(self.other_organizer, 'Side Cup')
        self.team = make_team('Code Warriors', self.hackathon, self.captain)
        self.team.add_member(self.member, role=TeamMembership.Role.MEMBER)
        self.other_team = make_team('Other Squad', self.hackathon, self.other)
        self.side_team = make_team('Side Squad', self.side, self.captain)

    def login(self, user):
        self.client.login(username=user.username, password='test-password')

    def form_data(self, **overrides):
        data = {
            'title': 'SmartHealth',
            'tagline': 'Care, clearly',
            'description': 'A platform that explains health data.',
            'hackathon': self.hackathon.pk,
            'team': self.team.pk,
            'technologies': 'Python, Django, python',
            'repo_url': 'https://github.com/example/smarthealth',
            'demo_url': 'https://demo.example.com',
            'progress': 40,
            'intent': 'save',
        }
        data.update(overrides)
        return data

    def make_project(self, **overrides):
        values = {
            'title': 'SmartHealth',
            'tagline': 'Care, clearly',
            'description': 'A platform that explains health data.',
            'hackathon': self.hackathon,
            'team': self.team,
            'submitted_by': self.captain,
            'status': Project.Status.IN_PROGRESS,
            'technologies': 'Python, Django',
            'repo_url': 'https://github.com/example/smarthealth',
            'progress': 40,
        }
        values.update(overrides)
        return Project.objects.create(**values)

    def test_normalize_technologies_drops_duplicates(self):
        self.assertEqual(normalize_technologies(' Django, python, django '), 'Django, python')
        self.assertEqual(normalize_technologies(''), '')

    def test_optional_fields_keep_direct_creation_working(self):
        """Prizes and evaluations create projects without the newer columns."""
        project = Project.objects.create(
            title='Legacy',
            description='Submitted project',
            hackathon=self.hackathon,
            team=self.other_team,
            submitted_by=self.other,
            status=Project.Status.EVALUATED,
        )
        self.assertEqual(project.tagline, '')
        self.assertEqual(project.technologies, '')
        self.assertEqual(project.progress, 0)
        self.assertNotIn(Project.Status.IN_PROGRESS, Project.RANKED_STATUSES)
        self.assertIn(Project.Status.EVALUATED, Project.RANKED_STATUSES)

    def test_team_must_belong_to_the_hackathon(self):
        project = Project(
            title='Mismatch',
            description='A project pointed at the wrong event.',
            hackathon=self.side,
            team=self.team,
            submitted_by=self.captain,
        )
        with self.assertRaises(ValidationError):
            project.full_clean()

    def test_anonymous_visitors_are_sent_to_login(self):
        response = self.client.get(reverse('projects:home'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)

    def test_captain_creates_an_in_progress_project(self):
        self.login(self.captain)
        response = self.client.post(reverse('projects:create'), self.form_data())
        project = Project.objects.get(team=self.team)
        self.assertRedirects(response, reverse('projects:detail', args=[project.pk]))
        self.assertEqual(project.status, Project.Status.IN_PROGRESS)
        self.assertEqual(project.technologies, 'Python, Django')
        self.assertEqual(project.submitted_by, self.captain)
        self.assertEqual(project.progress, 40)

    def test_save_as_draft_keeps_the_project_editable(self):
        self.login(self.captain)
        self.client.post(reverse('projects:create'), self.form_data(intent='draft', progress=10))
        project = Project.objects.get(team=self.team)
        self.assertEqual(project.status, Project.Status.DRAFT)
        self.assertFalse(project.is_locked)

    def test_member_can_edit_but_cannot_move_the_project_to_another_team(self):
        project = self.make_project()
        self.login(self.member)
        response = self.client.post(
            reverse('projects:edit', args=[project.pk]),
            self.form_data(title='Renamed', team=self.other_team.pk, hackathon=self.side.pk),
        )
        self.assertRedirects(response, reverse('projects:detail', args=[project.pk]))
        project.refresh_from_db()
        self.assertEqual(project.title, 'Renamed')
        self.assertEqual(project.team_id, self.team.id)
        self.assertEqual(project.hackathon_id, self.hackathon.id)

    def test_drafts_stay_private_and_submissions_are_visible_to_the_cohort(self):
        private = self.make_project(title='Private Draft')
        public = self.make_project(
            title='Public Entry', team=self.other_team, submitted_by=self.other,
            status=Project.Status.SUBMITTED,
        )
        self.login(self.other)
        listed = self.client.get(reverse('projects:home'))
        self.assertContains(listed, 'Public Entry')
        self.assertNotContains(listed, 'Private Draft')
        self.assertEqual(self.client.get(reverse('projects:detail', args=[private.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('projects:detail', args=[public.pk])).status_code, 200)

        self.login(self.jury)
        jury_list = self.client.get(reverse('projects:home'))
        self.assertContains(jury_list, 'Public Entry')
        self.assertNotContains(jury_list, 'Private Draft')

        self.login(self.organizer)
        organizer_list = self.client.get(reverse('projects:home'))
        self.assertContains(organizer_list, 'Private Draft')
        self.assertContains(organizer_list, 'Public Entry')

        self.login(self.other_organizer)
        self.assertEqual(
            self.client.get(reverse('projects:detail', args=[private.pk])).status_code, 404,
        )

    def test_only_the_captain_can_submit_and_submission_locks_the_project(self):
        project = self.make_project(status=Project.Status.DRAFT)
        self.login(self.member)
        self.assertEqual(self.client.post(reverse('projects:submit', args=[project.pk])).status_code, 403)

        self.login(self.captain)
        response = self.client.post(reverse('projects:submit', args=[project.pk]))
        self.assertRedirects(response, reverse('projects:detail', args=[project.pk]))
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.SUBMITTED)
        self.assertEqual(project.progress, 100)
        self.assertIsNotNone(project.submitted_at)
        self.assertEqual(self.client.get(reverse('projects:edit', args=[project.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse('projects:delete', args=[project.pk])).status_code, 403)

    def test_submit_requires_a_repository_and_an_open_window(self):
        project = self.make_project(repo_url='')
        self.login(self.captain)
        response = self.client.post(reverse('projects:submit', args=[project.pk]))
        self.assertEqual(response.status_code, 200)
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.IN_PROGRESS)

        upcoming = make_hackathon(
            self.organizer, 'Upcoming Cup',
            start=timedelta(days=2), end=timedelta(days=5),
            registration=timedelta(days=1), submission=timedelta(days=4),
        )
        upcoming_team = make_team('Early Squad', upcoming, self.captain)
        early = self.make_project(
            title='Early', hackathon=upcoming, team=upcoming_team,
            repo_url='https://github.com/example/early',
        )
        self.assertEqual(self.client.get(reverse('projects:submit', args=[early.pk])).status_code, 403)

    def test_closed_hackathon_cannot_open_a_project(self):
        late = User.objects.create_user(
            username='late', password='test-password', role=User.Role.PARTICIPANT,
        )
        closed = make_hackathon(
            self.organizer, 'Closed Cup',
            start=timedelta(days=-6), end=timedelta(days=-1),
            registration=timedelta(days=-7), submission=timedelta(days=-2),
        )
        make_team('Late Squad', closed, late)
        self.login(late)
        response = self.client.get(reverse('projects:create'))
        self.assertRedirects(response, reverse('projects:home'))
        self.assertEqual(Project.objects.filter(team__created_by=late).count(), 0)

    def test_organizer_can_review_reopen_and_mark_evaluated(self):
        project = self.make_project(status=Project.Status.SUBMITTED, progress=100)
        project.submitted_at = timezone.now()
        project.save(update_fields=['submitted_at'])
        self.login(self.organizer)

        self.client.post(reverse('projects:review', args=[project.pk]))
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.UNDER_REVIEW)

        self.assertEqual(
            self.client.post(reverse('projects:mark_evaluated', args=[project.pk])).status_code, 403,
        )
        criterion = EvaluationCriterion.objects.create(
            hackathon=self.hackathon, name='Innovation', weight=Decimal('100'),
        )
        evaluation = Evaluation.objects.create(
            project=project, jury=self.jury, is_complete=True, comment='private jury note',
        )
        EvaluationScore.objects.create(
            evaluation=evaluation, criterion=criterion, score=Decimal('8.50'),
        )
        self.client.post(reverse('projects:mark_evaluated', args=[project.pk]))
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.EVALUATED)

        detail = self.client.get(reverse('projects:detail', args=[project.pk]))
        self.assertContains(detail, '8.5/10')
        self.assertNotContains(detail, 'private jury note')

        self.login(self.captain)
        hidden = self.client.get(reverse('projects:detail', args=[project.pk]))
        self.assertNotContains(hidden, '8.5/10')
        self.assertNotContains(hidden, 'private jury note')
        self.hackathon.results_published = True
        self.hackathon.save(update_fields=['results_published'])
        published = self.client.get(reverse('projects:detail', args=[project.pk]))
        self.assertContains(published, '8.5/10')

    def test_ranked_project_cannot_be_reopened(self):
        project = self.make_project(status=Project.Status.SUBMITTED, progress=100)
        LeaderboardEntry.objects.create(
            hackathon=self.hackathon, project=project, rank=1, final_score=Decimal('8.00'),
        )
        self.login(self.organizer)
        self.assertEqual(self.client.post(reverse('projects:reopen', args=[project.pk])).status_code, 403)
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.SUBMITTED)

    def test_organizer_reopen_lets_the_team_edit_again(self):
        project = self.make_project(status=Project.Status.SUBMITTED, progress=100)
        self.login(self.organizer)
        self.client.post(reverse('projects:reopen', args=[project.pk]))
        project.refresh_from_db()
        self.assertEqual(project.status, Project.Status.IN_PROGRESS)
        self.assertIsNone(project.submitted_at)
        self.login(self.member)
        self.assertEqual(self.client.get(reverse('projects:edit', args=[project.pk])).status_code, 200)

    def test_captain_deletes_a_draft_only(self):
        project = self.make_project(status=Project.Status.DRAFT)
        self.login(self.member)
        self.assertEqual(self.client.post(reverse('projects:delete', args=[project.pk])).status_code, 403)
        self.login(self.captain)
        response = self.client.post(reverse('projects:delete', args=[project.pk]))
        self.assertRedirects(response, reverse('projects:home'))
        self.assertFalse(Project.objects.filter(pk=project.pk).exists())

    def test_duplicate_copies_text_into_a_team_without_a_project(self):
        project = self.make_project()
        self.login(self.captain)
        response = self.client.post(reverse('projects:duplicate', args=[project.pk]), {'team': self.side_team.pk})
        copy = Project.objects.get(team=self.side_team)
        self.assertRedirects(response, reverse('projects:edit', args=[copy.pk]))
        self.assertEqual(copy.status, Project.Status.DRAFT)
        self.assertEqual(copy.hackathon, self.side)
        self.assertTrue(copy.title.startswith('Copy of'))
        self.assertEqual(copy.technologies, project.technologies)
        self.assertFalse(copy.banner)

    def test_filters_and_export_respect_visibility(self):
        self.make_project(title='Private Draft', status=Project.Status.DRAFT)
        self.make_project(
            title='Public Entry', team=self.other_team, submitted_by=self.other,
            status=Project.Status.SUBMITTED, technologies='React',
        )
        self.login(self.captain)
        draft_tab = self.client.get(reverse('projects:home'), {'status': 'draft'})
        self.assertContains(draft_tab, 'Private Draft')
        self.assertNotContains(draft_tab, 'Public Entry')
        exported = self.client.get(reverse('projects:export')).content.decode('utf-8-sig')
        self.assertIn('Private Draft', exported)
        self.assertIn('Public Entry', exported)

        self.login(self.jury)
        jury_export = self.client.get(reverse('projects:export')).content.decode('utf-8-sig')
        self.assertIn('Public Entry', jury_export)
        self.assertNotIn('Private Draft', jury_export)

    def test_pitch_deck_must_be_a_pdf_and_banner_an_image(self):
        self.login(self.captain)
        rejected = self.client.post(reverse('projects:create'), {
            **self.form_data(intent='draft'),
            'pitch_deck': SimpleUploadedFile('notes.txt', b'hello', content_type='text/plain'),
        })
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(Project.objects.count(), 0)

        image = BytesIO()
        Image.new('RGB', (1, 1), 'red').save(image, format='PNG')
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                accepted = self.client.post(reverse('projects:create'), {
                    **self.form_data(intent='draft'),
                    'banner': SimpleUploadedFile('banner.png', image.getvalue(), content_type='image/png'),
                    'pitch_deck': SimpleUploadedFile('deck.pdf', b'%PDF-1.4', content_type='application/pdf'),
                })
        project = Project.objects.get(team=self.team)
        self.assertRedirects(accepted, reverse('projects:detail', args=[project.pk]))
        self.assertTrue(project.banner.name.endswith('.png'))
        self.assertTrue(project.pitch_deck.name.endswith('.pdf'))

    def test_oversized_banner_is_rejected(self):
        upload = SimpleUploadedFile('banner.png', b'x', content_type='image/png')
        upload.size = MAX_BANNER_BYTES + 1
        with self.assertRaises(ValidationError):
            _reject_oversized(upload, MAX_BANNER_BYTES, 'Banner')
