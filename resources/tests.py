from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from resources.models import Resource

User = get_user_model()
P = User.Role.PARTICIPANT
O = User.Role.ORGANIZER
J = User.Role.JURY


def make_user(username, role=P):
    return User.objects.create_user(
        username=username, email=f'{username}@ex.com', password='pass12345', role=role,
    )


class ResourcesBase(TestCase):
    def setUp(self):
        self.org = make_user('res_org', O)
        self.other_org = make_user('res_org2', O)
        self.participant = make_user('res_part', P)
        self.jury = make_user('res_jury', J)

        self.docs = Resource.objects.create(
            title='Django Guide', description='Backend basics for beginners.',
            type=Resource.Type.DOCUMENTATION, category=Resource.Category.WEB,
            url='https://docs.djangoproject.com', created_by=self.org,
        )
        self.tool = Resource.objects.create(
            title='Figma UI Kit', description='Reusable design tokens.',
            type=Resource.Type.TEMPLATE, category=Resource.Category.DESIGN,
            url='https://figma.com', created_by=self.org,
        )
        # Second resource is older so '-created_at' ordering is deterministic.
        self.video = Resource.objects.create(
            title='Pitching 101', description='How to present your project.',
            type=Resource.Type.VIDEO, category=Resource.Category.BUSINESS,
            url='https://youtu.be/x', created_by=self.other_org,
        )

    def login(self, user):
        self.client.force_login(user)

    def valid_data(self, **overrides):
        data = {
            'title': 'New Resource', 'description': 'Some text',
            'type': Resource.Type.TOOL, 'category': Resource.Category.GENERAL,
            'url': 'https://example.com/tool', 'file': '', 'image': '',
        }
        data.update(overrides)
        return data


class AuthGatingTests(ResourcesBase):
    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(reverse('resources:home'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.url)

    def test_list_renders_for_every_role(self):
        for user in (self.participant, self.jury, self.org):
            self.login(user)
            resp = self.client.get(reverse('resources:home'))
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, 'Django Guide')
            self.assertContains(resp, 'Figma UI Kit')


class FilteringSortingTests(ResourcesBase):
    def test_search_by_title(self):
        self.login(self.participant)
        resp = self.client.get(reverse('resources:home'), {'q': 'django'})
        titles = [r.title for r in resp.context['resources']]
        self.assertIn('Django Guide', titles)
        self.assertNotIn('Pitching 101', titles)

    def test_search_by_description(self):
        self.login(self.participant)
        resp = self.client.get(reverse('resources:home'), {'q': 'design tokens'})
        titles = [r.title for r in resp.context['resources']]
        self.assertEqual(titles, ['Figma UI Kit'])

    def test_filter_by_type_and_category(self):
        self.login(self.participant)
        resp = self.client.get(reverse('resources:home'), {'type': 'template'})
        self.assertEqual([r.title for r in resp.context['resources']], ['Figma UI Kit'])
        resp = self.client.get(reverse('resources:home'), {'category': 'business'})
        self.assertEqual([r.title for r in resp.context['resources']], ['Pitching 101'])

    def test_combined_search_and_filter(self):
        self.login(self.participant)
        resp = self.client.get(reverse('resources:home'), {'q': 'guide', 'category': 'web'})
        self.assertEqual([r.title for r in resp.context['resources']], ['Django Guide'])
        resp = self.client.get(reverse('resources:home'), {'q': 'guide', 'category': 'design'})
        self.assertEqual(list(resp.context['resources']), [])

    def test_invalid_type_ignored(self):
        self.login(self.participant)
        resp = self.client.get(reverse('resources:home'), {'type': 'not-a-type'})
        self.assertEqual(len(resp.context['resources']), 3)

    def test_sorting(self):
        self.login(self.participant)
        recent = [r.title for r in self.client.get(reverse('resources:home'), {'sort': 'recent'}).context['resources']]
        oldest = [r.title for r in self.client.get(reverse('resources:home'), {'sort': 'oldest'}).context['resources']]
        self.assertEqual(recent, list(reversed(oldest)))
        az = [r.title for r in self.client.get(reverse('resources:home'), {'sort': 'az'}).context['resources']]
        self.assertEqual(az, sorted(az))


class DetailTests(ResourcesBase):
    def test_detail_renders(self):
        self.login(self.participant)
        resp = self.client.get(reverse('resources:detail', args=[self.docs.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Django Guide')
        self.assertContains(resp, 'https://docs.djangoproject.com')

    def test_missing_detail_is_404(self):
        self.login(self.participant)
        resp = self.client.get(reverse('resources:detail', args=[999999]))
        self.assertEqual(resp.status_code, 404)


class CreateTests(ResourcesBase):
    def test_organizer_creates_resource(self):
        self.login(self.org)
        resp = self.client.post(reverse('resources:create'), self.valid_data(title='CI Cheat Sheet'))
        self.assertEqual(resp.status_code, 302)
        created = Resource.objects.get(title='CI Cheat Sheet')
        self.assertEqual(created.created_by, self.org)

    def test_participant_cannot_create(self):
        self.login(self.participant)
        resp = self.client.post(reverse('resources:create'), self.valid_data(title='Hijack'))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Resource.objects.filter(title='Hijack').exists())

    def test_jury_cannot_create(self):
        self.login(self.jury)
        self.client.post(reverse('resources:create'), self.valid_data(title='JuryRes'))
        self.assertFalse(Resource.objects.filter(title='JuryRes').exists())

    def test_validation_requires_asset(self):
        self.login(self.org)
        resp = self.client.post(reverse('resources:create'), self.valid_data(url=''))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('url', resp.context['form'].errors)

    def test_rejects_non_http_scheme(self):
        self.login(self.org)
        resp = self.client.post(reverse('resources:create'), self.valid_data(url='ftp://example.com'))
        self.assertIn('url', resp.context['form'].errors)
        self.assertFalse(Resource.objects.filter(url='ftp://example.com').exists())

    def test_rejects_bad_file_extension(self):
        self.login(self.org)
        bad = SimpleUploadedFile('tool.exe', b'MZ', content_type='application/octet-stream')
        resp = self.client.post(reverse('resources:create'), self.valid_data(url='', file=bad))
        self.assertIn('file', resp.context['form'].errors)

    def test_rejects_oversized_file(self):
        self.login(self.org)
        good = SimpleUploadedFile('guide.pdf', b'x' * 2048, content_type='application/pdf')
        with mock.patch('resources.forms.MAX_FILE_SIZE', 1024):
            resp = self.client.post(reverse('resources:create'), self.valid_data(url='', file=good))
        self.assertIn('file', resp.context['form'].errors)

    def test_accepts_allowed_file(self):
        self.login(self.org)
        good = SimpleUploadedFile('guide.pdf', b'%PDF-1.4', content_type='application/pdf')
        resp = self.client.post(reverse('resources:create'), self.valid_data(url='', file=good))
        self.assertRedirects(resp, reverse('resources:detail',
            args=[Resource.objects.get(title='New Resource').pk]))


class EditDeleteTests(ResourcesBase):
    def test_organizer_edits(self):
        self.login(self.org)
        resp = self.client.post(reverse('resources:edit', args=[self.docs.pk]),
                                self.valid_data(title='Django Guide', url='https://django.dev'))
        self.assertEqual(resp.status_code, 302)
        self.docs.refresh_from_db()
        self.assertEqual(self.docs.url, 'https://django.dev')

    def test_non_organizer_cannot_edit_direct_url(self):
        self.login(self.participant)
        resp = self.client.post(reverse('resources:edit', args=[self.docs.pk]),
                                self.valid_data(title='Changed'))
        self.assertEqual(resp.status_code, 302)
        self.docs.refresh_from_db()
        self.assertNotEqual(self.docs.title, 'Changed')

    def test_organizer_deletes_via_post(self):
        self.login(self.org)
        resp = self.client.post(reverse('resources:delete', args=[self.video.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Resource.objects.filter(pk=self.video.pk).exists())

    def test_delete_get_shows_confirmation(self):
        self.login(self.org)
        resp = self.client.get(reverse('resources:delete', args=[self.video.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Delete this resource')

    def test_participant_cannot_delete_direct_url(self):
        self.login(self.participant)
        resp = self.client.post(reverse('resources:delete', args=[self.video.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Resource.objects.filter(pk=self.video.pk).exists())


class CsrfTests(ResourcesBase):
    def test_create_rejected_without_csrf_token(self):
        from django.test import Client
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.org)
        resp = csrf_client.post(reverse('resources:create'), self.valid_data(title='No CSRF'))
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Resource.objects.filter(title='No CSRF').exists())


class ModelHelperTests(ResourcesBase):
    def test_action_labels_and_asset_kind(self):
        self.assertEqual(self.docs.asset_kind, 'url')
        self.assertTrue(self.docs.has_asset)
        file_res = Resource.objects.create(
            title='Zip pack', type=Resource.Type.TEMPLATE, category=Resource.Category.GENERAL,
            file=SimpleUploadedFile('pack.zip', b'PK', content_type='application/zip'),
            created_by=self.org,
        )
        self.assertEqual(file_res.asset_kind, 'file')
        self.assertEqual(file_res.action_label, 'Download')
