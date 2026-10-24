from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from PIL import Image

from accounts.forms import MAX_AVATAR_BYTES, PersonalInfoForm, RegisterForm

import io
import os

User = get_user_model()

# Passwords that satisfy AUTH_PASSWORD_VALIDATORS (length, not numeric,
# not common, not similar to the username).
GOOD_PW = 'StrongPass!2026'
NEW_PW = 'EvenStronger#2027'


def make_user(username='alice', role=User.Role.PARTICIPANT, **extra):
    return User.objects.create_user(
        username=username, email=f'{username}@example.com',
        password=GOOD_PW, role=role, **extra,
    )


# --------------------------------------------------------------------------
# Model behaviour (custom fields + properties)
# --------------------------------------------------------------------------
class UserModelTests(TestCase):
    def test_defaults_and_str(self):
        user = make_user()
        self.assertEqual(user.role, User.Role.PARTICIPANT)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(user.theme, 'light')
        self.assertEqual(user.language, 'en')
        self.assertTrue(user.notif_in_app)
        self.assertFalse(user.notif_weekly)
        self.assertIn('Participant', str(user))

    def test_skills_and_interests_default_empty(self):
        user = make_user()
        self.assertEqual(user.skills, '')
        self.assertEqual(user.interests, '')

    def test_initials(self):
        user = make_user(first_name='Kmar', last_name='Ben Mbarek')
        self.assertEqual(user.initials, 'KB')
        self.assertEqual(make_user(username='zz', first_name='', last_name='').initials, 'Z')

    def test_profile_completion(self):
        empty = User.objects.create_user(username='only', password=GOOD_PW)
        self.assertEqual(empty.profile_completion, 0)
        full = make_user(first_name='A', last_name='B')
        full.institution = 'Uni'
        full.field_of_study = 'CS'
        full.bio = 'hi'
        full.phone = '123'
        self.assertGreater(full.profile_completion, 0)


# --------------------------------------------------------------------------
# Registration form + view: role is never self-selectable
# --------------------------------------------------------------------------
class RegisterFormTests(TestCase):
    def base_data(self, **over):
        data = {
            'username': 'newbie', 'first_name': 'New', 'last_name': 'Bie',
            'email': 'newbie@example.com', 'password1': GOOD_PW, 'password2': GOOD_PW,
        }
        data.update(over)
        return data

    def test_no_role_field(self):
        self.assertNotIn('role', RegisterForm().fields)

    def test_valid_creates_participant_with_hashed_password(self):
        form = RegisterForm(data=self.base_data())
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertEqual(user.role, User.Role.PARTICIPANT)
        # Native Django hashing: raw password must not be stored in clear.
        self.assertNotEqual(user.password, GOOD_PW)
        self.assertTrue(user.check_password(GOOD_PW))

    def test_password_mismatch_rejected(self):
        form = RegisterForm(data=self.base_data(password2='Different!2026'))
        self.assertFalse(form.is_valid())
        self.assertIn('password2', form.errors)

    def test_duplicate_username_rejected(self):
        make_user(username='newbie')
        form = RegisterForm(data=self.base_data())
        self.assertFalse(form.is_valid())
        self.assertIn('username', form.errors)


class RegisterViewTests(TestCase):
    def url(self):
        return reverse('register')

    def test_get_anonymous_shows_form(self):
        resp = self.client.get(self.url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'registerForm')

    def test_authenticated_user_redirected(self):
        self.client.force_login(make_user())
        resp = self.client.get(self.url())
        self.assertEqual(resp.status_code, 302)

    def test_valid_signup_creates_participant_and_logs_in(self):
        resp = self.client.post(self.url(), {
            'username': 'newbie', 'first_name': 'New', 'last_name': 'Bie',
            'email': 'newbie@example.com', 'password1': GOOD_PW, 'password2': GOOD_PW,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], reverse('dashboard'))
        user = User.objects.get(username='newbie')
        self.assertEqual(user.role, User.Role.PARTICIPANT)
        # Now logged in: a private page is reachable.
        self.assertEqual(self.client.get(reverse('profile_settings')).status_code, 200)

    def test_injected_role_is_ignored(self):
        # The classic privilege-escalation attempt at signup.
        resp = self.client.post(self.url(), {
            'username': 'sneaky', 'first_name': 'S', 'last_name': 'K',
            'email': 'sneaky@example.com', 'password1': GOOD_PW, 'password2': GOOD_PW,
            'role': 'organizer',
        })
        self.assertEqual(resp.status_code, 302)
        user = User.objects.get(username='sneaky')
        self.assertEqual(user.role, User.Role.PARTICIPANT)

    def test_invalid_signup_no_user_created(self):
        resp = self.client.post(self.url(), {
            'username': 'x', 'email': 'not-an-email',
            'password1': GOOD_PW, 'password2': 'nope',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username='x').exists())


# --------------------------------------------------------------------------
# Login / logout (Django built-in views wired in config/urls.py)
# --------------------------------------------------------------------------
class AuthFlowTests(TestCase):
    def setUp(self):
        self.user = make_user(username='carol')

    def test_login_success(self):
        resp = self.client.post(reverse('login'),
                                {'username': 'carol', 'password': GOOD_PW})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], reverse('dashboard'))
        self.assertTrue(self.client.session.get('_auth_user_id'))

    def test_login_wrong_password(self):
        resp = self.client.post(reverse('login'),
                                {'username': 'carol', 'password': 'WrongPass!999'})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_logout(self):
        self.client.force_login(self.user)
        resp = self.client.post(reverse('logout'))
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('_auth_user_id', self.client.session)


# --------------------------------------------------------------------------
# Profile: access gating, allowed edits, escalation resistance, password change
# --------------------------------------------------------------------------
class ProfileAccessTests(TestCase):
    def url(self):
        return reverse('profile_settings')

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(self.url())
        self.assertEqual(resp.status_code, 302)
        self.assertIn('login', resp['Location'])

    def test_any_logged_in_user_can_view_own_profile(self):
        self.client.force_login(make_user(username='dave'))
        self.assertEqual(self.client.get(self.url()).status_code, 200)

    def _personal_data(self, user, **over):
        data = {
            'section': 'personal',
            'p-username': user.username,
            'p-first_name': user.first_name or 'F',
            'p-last_name': user.last_name or 'L',
            'p-email': user.email,
            'p-phone': '+21600000000',
            'p-institution': 'School of Tech',
            'p-field_of_study': 'CS',
            'p-bio': 'A short bio',
        }
        data.update(over)
        return data

    def test_can_update_allowed_fields(self):
        user = make_user(username='erin')
        self.client.force_login(user)
        resp = self.client.post(self.url(), self._personal_data(user))
        self.assertEqual(resp.status_code, 302)
        user.refresh_from_db()
        self.assertEqual(user.institution, 'School of Tech')
        self.assertEqual(user.phone, '+21600000000')

    def test_cannot_escalate_role_or_flags_via_profile(self):
        user = make_user(username='frank')
        self.client.force_login(user)
        payload = self._personal_data(user, **{
            'p-role': 'organizer', 'p-is_staff': 'on', 'p-is_superuser': 'on',
        })
        self.client.post(self.url(), payload)
        user.refresh_from_db()
        self.assertEqual(user.role, User.Role.PARTICIPANT)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_personal_info_form_has_no_sensitive_fields(self):
        fields = PersonalInfoForm(instance=make_user()).fields
        for forbidden in ('role', 'is_staff', 'is_superuser', 'user_permissions', 'groups'):
            self.assertNotIn(forbidden, fields)

    def test_password_change_keeps_session(self):
        user = make_user(username='gina')
        self.client.force_login(user)
        resp = self.client.post(self.url(), {
            'section': 'password',
            'pw-old_password': GOOD_PW,
            'pw-new_password1': NEW_PW,
            'pw-new_password2': NEW_PW,
        })
        self.assertEqual(resp.status_code, 302)
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PW))
        # update_session_auth_hash keeps the user logged in after the change.
        self.assertEqual(self.client.get(self.url()).status_code, 200)

    def test_password_change_rejects_wrong_current(self):
        user = make_user(username='hank')
        self.client.force_login(user)
        resp = self.client.post(self.url(), {
            'section': 'password',
            'pw-old_password': 'NotMyPass!123',
            'pw-new_password1': NEW_PW,
            'pw-new_password2': NEW_PW,
        })
        self.assertEqual(resp.status_code, 200)  # re-rendered with errors
        user.refresh_from_db()
        self.assertTrue(user.check_password(GOOD_PW))


# --------------------------------------------------------------------------
# Avatar upload validation (real content + size cap)
# --------------------------------------------------------------------------
class AvatarValidationTests(TestCase):
    def setUp(self):
        self.user = make_user(username='avataruser')

    def _form_with_avatar(self, upload):
        # The avatar field is cleaned independently of the (intentionally
        # omitted) required fields, so form.errors['avatar'] isolates avatar
        # validation without needing a fully valid form.
        form = PersonalInfoForm(instance=self.user, data={}, files={'avatar': upload})
        form.is_valid()
        return form

    def test_non_image_rejected_despite_png_mime(self):
        # Bytes are not an image, but the MIME is *declared* image/png: the
        # rejection must come from real image validation, not the MIME claim.
        fake = SimpleUploadedFile('fake.png', b'not really image bytes', 'image/png')
        errors = self._form_with_avatar(fake).errors.get('avatar')
        self.assertTrue(errors, 'fake image/png should be rejected')

    def test_oversized_image_rejected(self):
        # A genuine PNG whose real size exceeds MAX_AVATAR_BYTES.
        buf = io.BytesIO()
        width = height = 1200
        Image.frombytes('RGB', (width, height), os.urandom(width * height * 3)).save(buf, 'PNG')
        data = buf.getvalue()
        self.assertGreater(len(data), MAX_AVATAR_BYTES)  # sanity: truly oversized
        big = SimpleUploadedFile('big.png', data, 'image/png')
        errors = self._form_with_avatar(big).errors.get('avatar')
        self.assertTrue(errors, 'oversized avatar should be rejected')


# --------------------------------------------------------------------------
# Django admin access
# --------------------------------------------------------------------------
class AdminAccessTests(TestCase):
    def test_superuser_can_access_admin(self):
        User.objects.create_superuser(username='root', email='root@example.com',
                                      password=GOOD_PW)
        self.client.force_login(User.objects.get(username='root'))
        self.assertEqual(self.client.get(reverse('admin:index')).status_code, 200)

    def test_non_staff_cannot_access_admin(self):
        self.client.force_login(make_user(username='ivan'))
        resp = self.client.get(reverse('admin:index'))
        self.assertEqual(resp.status_code, 302)


# --------------------------------------------------------------------------
# Skills / interests on the profile
# --------------------------------------------------------------------------
class SkillsInterestsTests(TestCase):
    def _personal_data(self, user, **over):
        data = {
            'section': 'personal',
            'p-username': user.username,
            'p-first_name': user.first_name or 'F',
            'p-last_name': user.last_name or 'L',
            'p-email': user.email,
            'p-skills': 'Python, Django, SQL',
            'p-interests': 'AI, Web3',
        }
        data.update(over)
        return data

    def test_form_persists_skills_and_interests(self):
        user = make_user(username='gina')
        form = PersonalInfoForm(instance=user, data={
            'username': user.username, 'first_name': 'G', 'last_name': 'A',
            'email': user.email, 'skills': 'React, Figma', 'interests': 'Design',
        })
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        user.refresh_from_db()
        self.assertEqual(user.skills, 'React, Figma')
        self.assertEqual(user.interests, 'Design')

    def test_form_accepts_empty_skills_and_interests(self):
        user = make_user(username='hugo')
        form = PersonalInfoForm(instance=user, data={
            'username': user.username, 'first_name': 'H', 'last_name': 'U',
            'email': user.email, 'skills': '', 'interests': '',
        })
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        user.refresh_from_db()
        self.assertEqual(user.skills, '')
        self.assertEqual(user.interests, '')

    def test_profile_view_updates_and_redisplays(self):
        user = make_user(username='iris')
        self.client.force_login(user)
        resp = self.client.post(reverse('profile_settings'), self._personal_data(user))
        self.assertEqual(resp.status_code, 302)
        user.refresh_from_db()
        self.assertEqual(user.skills, 'Python, Django, SQL')
        self.assertEqual(user.interests, 'AI, Web3')
        # Re-displayed on the next GET.
        page = self.client.get(reverse('profile_settings'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Python, Django, SQL')
        self.assertContains(page, 'AI, Web3')

    def test_profile_view_update_replaces_previous_values(self):
        user = make_user(username='jack', skills='Old', interests='Old')
        self.client.force_login(user)
        self.client.post(reverse('profile_settings'),
                         self._personal_data(user, **{'p-skills': 'Go', 'p-interests': 'Systems'}))
        user.refresh_from_db()
        self.assertEqual(user.skills, 'Go')
        self.assertEqual(user.interests, 'Systems')
