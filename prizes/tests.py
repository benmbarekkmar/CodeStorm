from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.template.loader import get_template
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from evaluations.models import Evaluation, EvaluationCriterion, EvaluationScore
from hackathons.models import Hackathon
from projects.models import Project
from teams.models import Team

from .models import Certificate, LeaderboardEntry, Prize, PrizeAward, PrizeCategory, Sponsor
from .services import LeaderboardNotReady, compute_leaderboard, validate_prize_awards


def make_hackathon(organizer, title, **overrides):
    """A complete Hackathon as develop's model requires it."""
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


def make_team(name, hackathon, member, **overrides):
    values = {
        'name': name, 'hackathon': hackathon,
        'created_by': member, 'captain': member, 'max_members': 5,
    }
    values.update(overrides)
    return Team.objects.create(**values)


class PrizeWorkflowTests(TestCase):
    def setUp(self):
        self.organizer = User.objects.create_user(
            username='organizer', password='test-password', role=User.Role.ORGANIZER,
        )
        self.other_organizer = User.objects.create_user(
            username='other-organizer', password='test-password', role=User.Role.ORGANIZER,
        )
        self.participant = User.objects.create_user(
            username='participant', password='test-password', role=User.Role.PARTICIPANT,
        )
        self.other_participant = User.objects.create_user(
            username='other-participant', password='test-password', role=User.Role.PARTICIPANT,
        )
        self.jury = User.objects.create_user(
            username='jury', password='test-password', role=User.Role.JURY,
        )
        self.hackathon = make_hackathon(self.organizer, 'HackFlow Demo')
        self.other_hackathon = make_hackathon(self.other_organizer, 'Another Event')
        self.category, _created = PrizeCategory.objects.get_or_create(name='Grand Prize')
        self.criterion = EvaluationCriterion.objects.create(
            hackathon=self.hackathon,
            name='Innovation',
            weight=Decimal('100'),
        )
        self.project = self.make_project('First project', Decimal('8.50'))
        self.team = self.project.team
        self.team.members.add(self.participant)

    def make_project(self, title, score=None, status=Project.Status.EVALUATED, jury=None):
        participant = getattr(self, 'participant', None)
        if participant is None:
            participant = User.objects.create_user(
                username=f'participant-{title[:5]}', role=User.Role.PARTICIPANT,
            )
        team = make_team(f'Team {title}', self.hackathon, participant)
        project = Project.objects.create(
            title=title,
            description='Submitted project',
            hackathon=self.hackathon,
            team=team,
            submitted_by=participant,
            status=status,
        )
        if score is not None:
            evaluation = Evaluation.objects.create(
                project=project,
                jury=jury or self.jury,
                is_complete=True,
                comment='Private jury comment',
            )
            EvaluationScore.objects.create(
                evaluation=evaluation,
                criterion=self.criterion,
                score=score,
            )
        return project

    def create_prize(self, **overrides):
        values = {
            'name': 'Grand Prize',
            'hackathon': self.hackathon,
            'category': self.category,
            'amount': Decimal('500.00'),
        }
        values.update(overrides)
        return Prize.objects.create(**values)

    def organizer_login(self):
        self.client.force_login(self.organizer)

    def test_all_prize_templates_compile(self):
        from pathlib import Path

        template_dir = Path(__file__).resolve().parent.parent / 'templates' / 'prizes'
        for template_path in template_dir.glob('*.html'):
            with self.subTest(template=template_path.name):
                get_template(f'prizes/{template_path.name}')

    def test_organizer_prize_management_pages_render_from_real_data(self):
        prize = self.create_prize()
        sponsor = Sponsor.objects.create(name='Event Sponsor', created_by=self.organizer)
        prize.sponsor = sponsor
        prize.save(update_fields=('sponsor',))
        self.organizer_login()
        paths = (
            reverse('prizes:home'),
            reverse('prizes:prize_detail', args=(prize.pk,)),
            reverse('prizes:prize_assign_winner', args=(prize.pk,)),
            reverse('prizes:category_list'),
            reverse('prizes:category_edit', args=(self.category.pk,)),
            reverse('prizes:category_delete', args=(self.category.pk,)),
            reverse('prizes:sponsor_list'),
            reverse('prizes:sponsor_edit', args=(sponsor.pk,)),
            reverse('prizes:sponsor_create'),
            reverse('prizes:leaderboard_prepare', args=(self.hackathon.pk,)),
            reverse('prizes:leaderboard_publish', args=(self.hackathon.pk,)),
            reverse('prizes:certificate_list', args=(self.hackathon.pk,)),
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_valid_prize_creation_and_association(self):
        self.organizer_login()
        response = self.client.post(reverse('prizes:prize_create'), {
            'hackathon': self.hackathon.pk,
            'name': 'Best Innovation',
            'description': 'A notable idea',
            'category': self.category.pk,
            'amount': '250.00',
            'reward': 'Cloud credits',
            'winner_slots': '1',
            'eligibility_criteria': '',
            'sponsor': '',
        })
        self.assertEqual(response.status_code, 302)
        prize = Prize.objects.get(name='Best Innovation')
        self.assertEqual(prize.hackathon, self.hackathon)

    def test_invalid_amount_and_duplicate_name_are_rejected(self):
        prize = self.create_prize()
        self.organizer_login()
        response = self.client.post(reverse('prizes:prize_create'), {
            'hackathon': self.hackathon.pk,
            'name': 'Negative prize', 'description': '', 'category': '',
            'sponsor': '', 'amount': '-1.00', 'reward': '',
            'winner_slots': '1', 'eligibility_criteria': '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aria-invalid="true"', html=False)
        self.assertContains(response, 'aria-describedby="id_amount-errors"', html=False)
        self.assertFalse(Prize.objects.filter(name='Negative prize').exists())
        response = self.client.post(reverse('prizes:prize_create'), {
            'hackathon': self.hackathon.pk,
            'name': prize.name.lower(), 'description': '', 'category': '',
            'sponsor': '', 'amount': '', 'reward': '',
            'winner_slots': '1', 'eligibility_criteria': '',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Prize.objects.filter(hackathon=self.hackathon).count(), 1)

    def test_owner_can_edit_and_delete_unawarded_prize(self):
        prize = self.create_prize()
        self.organizer_login()
        response = self.client.post(reverse('prizes:prize_edit', args=(prize.pk,)), {
            'hackathon': self.hackathon.pk,
            'name': 'Updated award', 'description': '', 'category': self.category.pk,
            'sponsor': '', 'amount': '', 'reward': 'Mentoring',
            'winner_slots': '1', 'eligibility_criteria': '',
        })
        self.assertEqual(response.status_code, 302)
        prize.refresh_from_db()
        self.assertEqual(prize.name, 'Updated award')
        response = self.client.post(reverse('prizes:prize_delete', args=(prize.pk,)))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Prize.objects.filter(pk=prize.pk).exists())

    def test_category_crud_and_sponsor_scope(self):
        self.organizer_login()
        response = self.client.post(reverse('prizes:category_list'), {
            'name': 'Social Impact', 'description': 'Benefits the community',
            'icon': 'volunteer_activism', 'order': '2',
        })
        self.assertEqual(response.status_code, 302)
        category = PrizeCategory.objects.get(name='Social Impact')
        response = self.client.post(reverse('prizes:category_edit', args=(category.pk,)), {
            'name': 'Community Impact', 'description': '', 'icon': '', 'order': '2',
        })
        self.assertEqual(response.status_code, 302)
        sponsor_response = self.client.post(reverse('prizes:sponsor_create'), {
            'name': 'Public Sponsor', 'description': 'Sponsor profile',
            'website': 'https://example.test', 'contact_email': 'hello@example.test',
        })
        self.assertEqual(sponsor_response.status_code, 302)
        sponsor = Sponsor.objects.get(name='Public Sponsor')
        self.assertEqual(sponsor.created_by, self.organizer)
        self.create_prize(name='Sponsored Prize', sponsor=sponsor, category=category)
        self.client.force_login(self.other_organizer)
        self.assertEqual(self.client.get(
            reverse('prizes:sponsor_edit', args=(sponsor.pk,))
        ).status_code, 403)
        self.assertEqual(self.client.get(
            reverse('prizes:category_edit', args=(category.pk,))
        ).status_code, 403)

    def test_participant_sees_only_their_event_prizes_and_jury_scope_is_assigned(self):
        self.create_prize()
        Prize.objects.create(
            name='Other event prize', hackathon=self.other_hackathon,
            category=self.category,
        )
        self.client.force_login(self.participant)
        response = self.client.get(reverse('prizes:home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Grand Prize')
        self.assertNotContains(response, 'Other event prize')
        self.hackathon.results_published = True
        self.hackathon.save(update_fields=('results_published',))
        self.client.force_login(self.jury)
        response = self.client.get(reverse('prizes:home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.hackathon.title)
        self.assertNotContains(response, self.other_hackathon.title)

    def test_participant_and_jury_cannot_manage_prizes(self):
        prize = self.create_prize()
        for user in (self.participant, self.jury):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('prizes:prize_create')).status_code, 403)
            self.assertEqual(self.client.post(reverse('prizes:prize_edit', args=(prize.pk,))).status_code, 403)

    def test_organizer_cannot_access_another_hackathons_prize_or_project(self):
        other_prize = self.create_prize(name='Other prize', hackathon=self.other_hackathon)
        self.organizer_login()
        self.assertEqual(self.client.get(reverse('prizes:prize_detail', args=(other_prize.pk,))).status_code, 403)
        self.assertEqual(self.client.get(
            reverse('prizes:home'), {'hackathon': self.other_hackathon.pk}
        ).status_code, 403)

    def test_prize_award_requires_project_in_same_saved_ranking(self):
        prize = self.create_prize()
        foreign_team = make_team(
            'Foreign team', self.other_hackathon, self.participant,
        )
        foreign_project = Project.objects.create(
            title='Foreign project', description='Other event',
            hackathon=self.other_hackathon, team=foreign_team,
            submitted_by=self.participant, status=Project.Status.EVALUATED,
        )
        self.organizer_login()
        response = self.client.post(reverse('prizes:prize_assign_winner', args=(prize.pk,)), {
            'projects': [foreign_project.pk],
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PrizeAward.objects.filter(prize=prize).exists())
        LeaderboardEntry.objects.create(
            hackathon=self.hackathon, project=self.project, rank=1,
            final_score=Decimal('8.50'),
        )
        response = self.client.post(reverse('prizes:prize_assign_winner', args=(prize.pk,)), {
            'projects': [self.project.pk],
        })
        self.assertEqual(response.status_code, 302)
        prize.refresh_from_db()
        self.assertEqual(prize.winning_project, self.project)
        self.assertEqual(prize.status, Prize.Status.ASSIGNED)

    def test_multiple_winners_are_limited_by_configured_slots(self):
        prize = self.create_prize(name='Shared Prize', winner_slots=2)
        second = self.make_project('Second winner', Decimal('8.00'))
        third = self.make_project('Third candidate', Decimal('7.50'))
        for rank, project, score in (
            (1, self.project, Decimal('8.50')),
            (2, second, Decimal('8.00')),
            (3, third, Decimal('7.50')),
        ):
            LeaderboardEntry.objects.create(
                hackathon=self.hackathon, project=project, rank=rank, final_score=score,
            )
        self.organizer_login()
        path = reverse('prizes:prize_assign_winner', args=(prize.pk,))
        response = self.client.post(path, {'projects': [self.project.pk, second.pk]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(prize.awards.count(), 2)
        response = self.client.post(path, {'projects': [self.project.pk, second.pk, third.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(prize.awards.count(), 2)

    def test_model_rejects_cross_hackathon_legacy_winner(self):
        foreign_team = make_team(
            'Foreign', self.other_hackathon, self.participant,
        )
        foreign_project = Project.objects.create(
            title='Foreign', description='Other', hackathon=self.other_hackathon,
            team=foreign_team, submitted_by=self.participant,
        )
        prize = Prize(name='Invalid', hackathon=self.hackathon, winning_project=foreign_project)
        with self.assertRaises(ValidationError):
            prize.full_clean()

    def test_leaderboard_and_award_models_reject_inconsistent_projects(self):
        foreign_team = make_team(
            'Foreign ranking team', self.other_hackathon, self.other_participant,
        )
        foreign_project = Project.objects.create(
            title='Foreign ranking project', description='Other event',
            hackathon=self.other_hackathon, team=foreign_team,
            submitted_by=self.other_participant, status=Project.Status.EVALUATED,
        )
        entry = LeaderboardEntry(
            hackathon=self.hackathon, project=foreign_project, rank=1,
            final_score=Decimal('8.50'),
        )
        with self.assertRaises(ValidationError):
            entry.full_clean()

        prize = self.create_prize()
        award = PrizeAward(prize=prize, project=foreign_project, position=1)
        with self.assertRaises(ValidationError):
            award.full_clean()

        invalid_score = LeaderboardEntry(
            hackathon=self.hackathon, project=self.project, rank=1,
            final_score=Decimal('10.01'),
        )
        with self.assertRaises(ValidationError):
            invalid_score.full_clean()

    def test_prize_award_must_match_legacy_winner_and_slots_before_publication(self):
        prize = self.create_prize()
        LeaderboardEntry.objects.create(
            hackathon=self.hackathon, project=self.project, rank=1,
            final_score=Decimal('8.50'),
        )
        prize.awards.create(project=self.project, position=1)
        prize.status = Prize.Status.ASSIGNED
        prize.save(update_fields=('status',))

        with self.assertRaises(LeaderboardNotReady):
            validate_prize_awards(self.hackathon)

        prize.winning_project = self.project
        prize.save(update_fields=('winning_project',))
        validate_prize_awards(self.hackathon)

        unranked_project = self.make_project('Unranked project', Decimal('7.50'))
        unranked_prize = self.create_prize(name='Unranked award')
        unranked_prize.awards.create(project=unranked_project, position=1)
        unranked_prize.winning_project = unranked_project
        unranked_prize.status = Prize.Status.ASSIGNED
        unranked_prize.save(update_fields=('winning_project', 'status'))
        with self.assertRaises(LeaderboardNotReady):
            validate_prize_awards(self.hackathon)

    def test_certificate_model_rejects_unpublished_results_and_foreign_prize(self):
        certificate = Certificate(
            recipient=self.participant,
            hackathon=self.hackathon,
            cert_type=Certificate.CertType.WINNER,
            rank=1,
        )
        with self.assertRaises(ValidationError) as error:
            certificate.full_clean()
        self.assertIn('hackathon', error.exception.message_dict)

        self.hackathon.results_published = True
        self.hackathon.save(update_fields=('results_published',))
        foreign_prize = self.create_prize(
            name='Foreign event prize', hackathon=self.other_hackathon,
            status=Prize.Status.AWARDED,
        )
        certificate.prize = foreign_prize
        certificate.cert_type = Certificate.CertType.SPECIAL_AWARD
        with self.assertRaises(ValidationError) as error:
            certificate.full_clean()
        self.assertIn('prize', error.exception.message_dict)

    def test_weighted_rank_has_explicit_competition_ties(self):
        second = self.make_project('Second project', Decimal('8.50'))
        third = self.make_project('Third project', Decimal('7.25'))
        ranked = compute_leaderboard(self.hackathon)
        self.assertEqual([(row['project'], row['rank']) for row in ranked], [
            (self.project, 1), (second, 1), (third, 3),
        ])
        self.assertEqual(ranked[0]['score'], Decimal('8.50'))

    def test_incomplete_evaluations_block_preparation_and_publication(self):
        evaluation = self.project.evaluations.first()
        evaluation.is_complete = False
        evaluation.save(update_fields=('is_complete',))
        with self.assertRaises(LeaderboardNotReady):
            compute_leaderboard(self.hackathon)
        self.organizer_login()
        response = self.client.post(
            reverse('prizes:leaderboard_prepare', args=(self.hackathon.pk,)),
            {'action': 'save_leaderboard'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(LeaderboardEntry.objects.filter(hackathon=self.hackathon).exists())
        response = self.client.post(
            reverse('prizes:leaderboard_publish', args=(self.hackathon.pk,)),
            {'confirm': 'yes'},
        )
        self.assertEqual(response.status_code, 200)
        self.hackathon.refresh_from_db()
        self.assertFalse(self.hackathon.results_published)

    def test_publishing_requires_a_current_complete_draft_and_hides_jury_comments(self):
        self.organizer_login()
        self.client.post(
            reverse('prizes:leaderboard_prepare', args=(self.hackathon.pk,)),
            {'action': 'save_leaderboard'},
        )
        response = self.client.post(
            reverse('prizes:leaderboard_publish', args=(self.hackathon.pk,)),
            {'confirm': 'yes'},
        )
        self.assertEqual(response.status_code, 302)
        self.hackathon.refresh_from_db()
        self.assertTrue(self.hackathon.results_published)
        prize = self.create_prize()
        prize.awards.create(project=self.project, position=1)
        prize.winning_project = self.project
        prize.status = Prize.Status.AWARDED
        prize.save()
        self.client.force_login(self.participant)
        response = self.client.get(reverse('prizes:home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'First project')
        self.assertNotContains(response, 'Private jury comment')

    def test_stale_saved_ranking_cannot_be_published(self):
        self.organizer_login()
        self.client.post(
            reverse('prizes:leaderboard_prepare', args=(self.hackathon.pk,)),
            {'action': 'save_leaderboard'},
        )
        EvaluationScore.objects.filter(evaluation__project=self.project).update(score=Decimal('9.00'))
        response = self.client.post(
            reverse('prizes:leaderboard_publish', args=(self.hackathon.pk,)),
            {'confirm': 'yes'},
        )
        self.assertEqual(response.status_code, 200)
        self.hackathon.refresh_from_db()
        self.assertFalse(self.hackathon.results_published)

    def test_certificates_require_official_results_and_are_private(self):
        self.organizer_login()
        path = reverse('prizes:certificate_generate', args=(self.hackathon.pk,))
        self.assertEqual(self.client.post(path, {'confirm': 'yes'}).status_code, 302)
        self.client.post(
            reverse('prizes:leaderboard_prepare', args=(self.hackathon.pk,)),
            {'action': 'save_leaderboard'},
        )
        self.client.post(
            reverse('prizes:leaderboard_publish', args=(self.hackathon.pk,)),
            {'confirm': 'yes'},
        )
        self.assertEqual(self.client.get(path).status_code, 200)
        self.client.post(path, {'confirm': 'yes'})
        self.assertEqual(self.client.get(
            reverse('prizes:certificate_list', args=(self.hackathon.pk,))
        ).status_code, 200)
        certificate = Certificate.objects.get(recipient=self.participant)
        self.client.force_login(self.participant)
        response = self.client.get(reverse('prizes:certificate_download', args=(certificate.uid,)))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response.content.startswith(b'%PDF'))
        self.assertEqual(self.client.get(
            reverse('prizes:certificate_preview', args=(certificate.uid,))
        ).status_code, 200)
        self.client.force_login(self.other_participant)
        self.assertEqual(self.client.get(
            reverse('prizes:certificate_download', args=(certificate.uid,))
        ).status_code, 403)

    def test_unpublish_is_post_only_and_blocked_after_certificates(self):
        self.hackathon.results_published = True
        self.hackathon.save(update_fields=('results_published',))
        self.organizer_login()
        path = reverse('prizes:leaderboard_unpublish', args=(self.hackathon.pk,))
        self.assertEqual(self.client.get(path).status_code, 200)
        Certificate.objects.create(
            recipient=self.participant, hackathon=self.hackathon,
            cert_type=Certificate.CertType.PARTICIPATION,
        )
        self.assertEqual(self.client.post(path, {'confirm': 'yes'}).status_code, 302)
        self.hackathon.refresh_from_db()
        self.assertTrue(self.hackathon.results_published)

    def test_unauthenticated_requests_redirect_to_login(self):
        response = self.client.get(reverse('prizes:home'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response.url)

    def test_sensitive_forms_enforce_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.organizer)
        response = csrf_client.post(reverse('prizes:prize_create'), {
            'hackathon': self.hackathon.pk,
            'name': 'No CSRF', 'description': '', 'category': self.category.pk,
            'sponsor': '', 'amount': '', 'reward': '',
            'winner_slots': '1', 'eligibility_criteria': '',
        })
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Prize.objects.filter(name='No CSRF').exists())


class ResultsVisibilityTests(TestCase):
    """Draft results, rankings and certificates stay private until publication."""

    def setUp(self):
        self.organizer = User.objects.create_user(
            username='vis-organizer', password='test-password', role=User.Role.ORGANIZER)
        self.participant = User.objects.create_user(
            username='vis-participant', password='test-password', role=User.Role.PARTICIPANT)
        self.jury = User.objects.create_user(
            username='vis-jury', password='test-password', role=User.Role.JURY)
        self.hackathon = make_hackathon(self.organizer, 'Visibility Event')
        self.category, _created = PrizeCategory.objects.get_or_create(name='Visibility Prize')
        self.criterion = EvaluationCriterion.objects.create(
            hackathon=self.hackathon, name='Innovation', weight=Decimal('100'))
        self.team = make_team('Visibility team', self.hackathon, self.participant)
        self.project = Project.objects.create(
            title='Visible project', description='A judged submission',
            hackathon=self.hackathon, team=self.team,
            submitted_by=self.participant, status=Project.Status.EVALUATED)
        evaluation = Evaluation.objects.create(
            project=self.project, jury=self.jury, is_complete=True)
        EvaluationScore.objects.create(
            evaluation=evaluation, criterion=self.criterion, score=Decimal('9.00'))
        self.client.force_login(self.organizer)
        self.client.post(
            reverse('prizes:leaderboard_prepare', args=(self.hackathon.pk,)),
            {'action': 'save_leaderboard'})
        self.prize = Prize.objects.create(
            name='Top prize', hackathon=self.hackathon, category=self.category)
        self.prize.awards.create(project=self.project, position=1)
        self.prize.winning_project = self.project
        self.prize.status = Prize.Status.ASSIGNED
        self.prize.save()

    def publish(self):
        self.client.force_login(self.organizer)
        response = self.client.post(
            reverse('prizes:leaderboard_publish', args=(self.hackathon.pk,)),
            {'confirm': 'yes'})
        self.assertEqual(response.status_code, 302)
        self.hackathon.refresh_from_db()
        self.assertTrue(self.hackathon.results_published)
        self.prize.refresh_from_db()
        self.assertEqual(self.prize.status, Prize.Status.AWARDED)

    def participant_home(self):
        self.client.force_login(self.participant)
        response = self.client.get(reverse('prizes:home'))
        self.assertEqual(response.status_code, 200)
        return response

    def test_draft_ranking_and_winners_stay_hidden_before_publication(self):
        self.assertTrue(LeaderboardEntry.objects.filter(hackathon=self.hackathon).exists())
        response = self.participant_home()
        self.assertNotContains(response, 'Official leaderboard')
        self.assertNotContains(response, 'Awarded to')
        self.assertNotContains(response, 'Visible project')
        self.assertNotContains(response, '9.00')
        self.assertContains(response, 'Top prize')

    def test_publication_reveals_the_official_ranking_and_award_recipients(self):
        self.publish()
        response = self.participant_home()
        self.assertContains(response, 'Published results')
        self.assertContains(response, 'Official leaderboard')
        self.assertContains(response, 'Visible project')
        self.assertContains(response, 'Awarded to')

    def test_certificate_is_inaccessible_while_results_are_unpublished(self):
        # Written around the model validation on purpose: the gate under test is
        # the view's, not Certificate.clean's.
        certificate = Certificate.objects.create(
            recipient=self.participant, hackathon=self.hackathon,
            cert_type=Certificate.CertType.PARTICIPATION)
        response = self.participant_home()
        self.assertNotContains(response, 'HF-')
        self.client.force_login(self.participant)
        self.assertEqual(self.client.get(
            reverse('prizes:certificate_download', args=(certificate.uid,))
        ).status_code, 403)
        self.assertEqual(self.client.get(
            reverse('prizes:certificate_preview', args=(certificate.uid,))
        ).status_code, 403)

    def test_unpublish_is_blocked_once_certificates_exist(self):
        self.publish()
        self.client.post(
            reverse('prizes:certificate_generate', args=(self.hackathon.pk,)),
            {'confirm': 'yes'})
        self.assertTrue(Certificate.objects.filter(hackathon=self.hackathon).exists())
        self.client.force_login(self.organizer)
        self.client.post(
            reverse('prizes:leaderboard_unpublish', args=(self.hackathon.pk,)),
            {'confirm': 'yes'})
        self.hackathon.refresh_from_db()
        self.assertTrue(self.hackathon.results_published)
        self.assertIn('Published results', self.participant_home().content.decode())
