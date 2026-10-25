from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from hackathons.models import Hackathon
from teams.models import Team


def normalize_technologies(value):
    """Trim a comma-separated technology list and drop duplicates."""
    parts = []
    seen = set()
    for raw in str(value or '').split(','):
        name = ' '.join(raw.split())
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            parts.append(name)
    return ', '.join(parts)


class Project(models.Model):
    """
    Projet soumis par une équipe dans le cadre d'un hackathon.
    Chaque équipe ne peut soumettre qu'un seul projet par hackathon.

    Columns added after the initial model are optional and have defaults.
    ``prizes`` and ``evaluations`` create projects through the ORM, so those
    call sites keep working without knowing about the newer fields.
    """

    class Status(models.TextChoices):
        DRAFT = 'draft', _('Draft')
        IN_PROGRESS = 'in_progress', _('In Progress')
        SUBMITTED = 'submitted', _('Submitted')
        UNDER_REVIEW = 'under_review', _('Under Evaluation')
        EVALUATED = 'evaluated', _('Evaluated')

    # Statuses a team can still edit. The values already stored by prizes
    # (submitted / under_review / evaluated) stay unchanged.
    EDITABLE_STATUSES = (Status.DRAFT, Status.IN_PROGRESS)
    # Statuses the prizes module is willing to rank.
    RANKED_STATUSES = (Status.SUBMITTED, Status.UNDER_REVIEW, Status.EVALUATED)

    title = models.CharField(_('Title'), max_length=200)
    tagline = models.CharField(_('Short pitch'), max_length=180, blank=True, default='')
    description = models.TextField(_('Description'))
    hackathon = models.ForeignKey(
        Hackathon,
        on_delete=models.CASCADE,
        related_name='projects',
        verbose_name=_('Hackathon'),
    )
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name='projects',
        verbose_name=_('Team'),
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='submitted_projects',
        verbose_name=_('Submitted by'),
    )
    status = models.CharField(
        _('Status'),
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    technologies = models.CharField(
        _('Technologies'),
        max_length=300,
        blank=True,
        default='',
        help_text=_('Comma-separated technologies, for example: Python, Django, PostgreSQL.'),
    )
    progress = models.PositiveSmallIntegerField(
        _('Progress'),
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    repo_url = models.URLField(_('Repository URL'), blank=True)
    demo_url = models.URLField(_('Demo URL'), blank=True)
    banner = models.ImageField(
        _('Banner'),
        upload_to='projects/banners/',
        blank=True,
        null=True,
    )
    pitch_deck = models.FileField(
        _('Pitch deck'),
        upload_to='projects/decks/',
        blank=True,
        null=True,
        validators=[FileExtensionValidator(['pdf'])],
    )

    submitted_at = models.DateTimeField(_('Submitted at'), null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-submitted_at', '-created_at']
        verbose_name = _('Project')
        verbose_name_plural = _('Projects')
        unique_together = [('team', 'hackathon')]

    def __str__(self):
        return f"{self.title} — {self.team.name}"

    def clean(self):
        super().clean()
        errors = {}
        if self.team_id and self.hackathon_id:
            team_hackathon_id = self.team.hackathon_id
            if team_hackathon_id is None:
                errors['team'] = _('This team is not registered for a hackathon.')
            elif team_hackathon_id != self.hackathon_id:
                errors['hackathon'] = _('The team must belong to the selected hackathon.')
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.title = (self.title or '').strip()
        self.tagline = (self.tagline or '').strip()
        self.technologies = normalize_technologies(self.technologies)
        super().save(*args, **kwargs)

    @property
    def is_locked(self):
        return self.status not in self.EDITABLE_STATUSES

    @property
    def tech_list(self):
        if not self.technologies:
            return []
        return [part.strip() for part in self.technologies.split(',') if part.strip()]
