from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _


class User(AbstractUser):
    class Role(models.TextChoices):
        PARTICIPANT = 'participant', _('Participant')
        ORGANIZER = 'organizer', _('Organizer')
        JURY = 'jury', _('Jury / Mentor')

    class Theme(models.TextChoices):
        LIGHT = 'light', _('Light')
        DARK = 'dark', _('Dark')
        SYSTEM = 'system', _('System')

    class Density(models.TextChoices):
        COMFORTABLE = 'comfortable', _('Comfortable')
        COMPACT = 'compact', _('Compact')

    class Language(models.TextChoices):
        EN = 'en', 'English'
        FR = 'fr', 'Français'

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.PARTICIPANT,
    )

    phone = models.CharField('Phone number', max_length=30, blank=True)
    institution = models.CharField('University / Institution', max_length=150, blank=True)
    field_of_study = models.CharField('Field of study', max_length=150, blank=True)
    bio = models.TextField('Short biography', blank=True, max_length=250)
    skills = models.TextField('Skills', blank=True, default='')
    interests = models.TextField('Interests', blank=True, default='')
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)

    theme = models.CharField(max_length=10, choices=Theme.choices, default=Theme.LIGHT)
    accent = models.CharField(max_length=10, default='purple')
    density = models.CharField(max_length=12, choices=Density.choices, default=Density.COMFORTABLE)
    language = models.CharField(max_length=10, choices=Language.choices, default=Language.EN)

    notif_in_app = models.BooleanField(default=True)
    notif_email = models.BooleanField(default=True)
    notif_hackathon = models.BooleanField(default=True)
    notif_team = models.BooleanField(default=True)
    notif_deadlines = models.BooleanField(default=True)
    notif_eval = models.BooleanField(default=True)
    notif_prizes = models.BooleanField(default=True)
    notif_weekly = models.BooleanField(default=False)

    @property
    def initials(self):
        first = (self.first_name or self.username or '')[:1]
        last = (self.last_name or '')[:1]
        return (first + last).upper() or 'U'

    @property
    def profile_completion(self):
        fields = [
            self.first_name, self.last_name, self.email, self.phone,
            self.institution, self.field_of_study, self.bio, bool(self.avatar),
        ]
        filled = sum(1 for value in fields if value)
        return round(filled / len(fields) * 100)

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"