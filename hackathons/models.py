from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Hackathon(models.Model):
    class Category(models.TextChoices):
        AI = 'ai', _('AI & Machine Learning')
        WEB = 'web', _('Web Development')
        FINTECH = 'fintech', _('FinTech')
        GREENTECH = 'greentech', _('GreenTech')
        CYBER = 'cyber', _('Cybersecurity')

    class Format(models.TextChoices):
        ONLINE = 'online', _('Online')
        IN_PERSON = 'in_person', _('In person')
        HYBRID = 'hybrid', _('Hybrid')

    class Status(models.TextChoices):
        DRAFT = 'draft', _('Draft')
        PUBLISHED = 'published', _('Published')
        CANCELLED = 'cancelled', _('Cancelled')

    organizer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='organized_hackathons',
    )
    title = models.CharField(_('Title'), max_length=150)
    description = models.TextField(_('Description'), max_length=2000)
    category = models.CharField(_('Category'), max_length=20, choices=Category.choices)
    event_format = models.CharField(_('Format'), max_length=20, choices=Format.choices, default=Format.ONLINE)
    location = models.CharField(_('Location'), max_length=200, blank=True)
    banner = models.ImageField(_('Banner'), upload_to='hackathons/', blank=True, null=True)

    start_date = models.DateTimeField(_('Start'))
    end_date = models.DateTimeField(_('End'))
    registration_deadline = models.DateTimeField(_('Registration deadline'))
    submission_deadline = models.DateTimeField(_('Submission deadline'))

    max_participants = models.PositiveIntegerField(_('Max participants'), default=150)
    min_team_size = models.PositiveSmallIntegerField(_('Min team size'), default=2)
    max_team_size = models.PositiveSmallIntegerField(_('Max team size'), default=5)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-start_date']

    def __str__(self):
        return self.title

    # ---- règles métier / validation ----
    def clean(self):
        errors = {}
        s, e = self.start_date, self.end_date
        reg, sub = self.registration_deadline, self.submission_deadline
        if s and e and e <= s:
            errors['end_date'] = _('The end date must be after the start date.')
        if reg and s and reg > s:
            errors['registration_deadline'] = _('Registration must close before the hackathon starts.')
        if sub and s and e and not (s <= sub <= e):
            errors['submission_deadline'] = _('The submission deadline must be between the start and the end.')
        if self.min_team_size and self.max_team_size and self.min_team_size > self.max_team_size:
            errors['max_team_size'] = _('Max team size must be greater than or equal to min team size.')
        if self.event_format != self.Format.ONLINE and not self.location:
            errors['location'] = _('A location is required for in-person or hybrid events.')
        if errors:
            raise ValidationError(errors)

    # ---- états calculés ----
    @property
    def phase(self):
        if self.status == self.Status.CANCELLED:
            return 'cancelled'
        if self.status == self.Status.DRAFT:
            return 'draft'
        now = timezone.now()
        if now < self.start_date:
            return 'upcoming'
        if now <= self.end_date:
            return 'ongoing'
        return 'completed'

    @property
    def registration_open(self):
        return self.status == self.Status.PUBLISHED and timezone.now() <= self.registration_deadline

    @property
    def submission_open(self):
        return (self.status == self.Status.PUBLISHED
                and self.start_date <= timezone.now() <= self.submission_deadline)


class Challenge(models.Model):
    class Difficulty(models.TextChoices):
        EASY = 'easy', _('Easy')
        MEDIUM = 'medium', _('Medium')
        HARD = 'hard', _('Hard')

    hackathon = models.ForeignKey(Hackathon, on_delete=models.CASCADE, related_name='challenges')
    title = models.CharField(_('Title'), max_length=150)
    description = models.TextField(_('Description'), max_length=2000)
    difficulty = models.CharField(max_length=10, choices=Difficulty.choices, default=Difficulty.MEDIUM)
    deadline = models.DateTimeField(_('Deadline'), null=True, blank=True)
    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.title} ({self.hackathon})'

    def clean(self):
        if self.deadline and self.hackathon_id:
            h = self.hackathon
            if not (h.start_date <= self.deadline <= h.end_date):
                raise ValidationError({'deadline': _('The challenge deadline must fall within the hackathon dates.')})
