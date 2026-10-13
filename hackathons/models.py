from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _


class Hackathon(models.Model):
    """
    Représente un hackathon organisé sur la plateforme.
    Un organisateur peut créer et gérer plusieurs hackathons.
    """

    class Status(models.TextChoices):
        DRAFT = 'draft', _('Draft')
        OPEN = 'open', _('Open for Registration')
        IN_PROGRESS = 'in_progress', _('In Progress')
        EVALUATION = 'evaluation', _('Under Evaluation')
        COMPLETED = 'completed', _('Completed')
        CANCELLED = 'cancelled', _('Cancelled')

    name = models.CharField(_('Name'), max_length=200)
    description = models.TextField(_('Description'), blank=True)
    status = models.CharField(
        _('Status'),
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
    )

    organizer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='organized_hackathons',
        limit_choices_to={'role': 'organizer'},
        verbose_name=_('Organizer'),
    )

    start_date = models.DateField(_('Start date'), null=True, blank=True)
    end_date = models.DateField(_('End date'), null=True, blank=True)

    max_teams = models.PositiveIntegerField(_('Maximum number of teams'), default=50)
    max_team_size = models.PositiveIntegerField(_('Maximum team size'), default=5)

    results_published = models.BooleanField(
        _('Results published'),
        default=False,
        help_text=_('When True, participants can view the leaderboard and their prizes.'),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = _('Hackathon')
        verbose_name_plural = _('Hackathons')

    def __str__(self):
        return self.name

    @property
    def is_completed(self):
        return self.status == self.Status.COMPLETED
