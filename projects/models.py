from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _

from hackathons.models import Hackathon
from teams.models import Team


class Project(models.Model):
    """
    Projet soumis par une équipe dans le cadre d'un hackathon.
    Chaque équipe ne peut soumettre qu'un seul projet par hackathon.
    """

    class Status(models.TextChoices):
        DRAFT = 'draft', _('Draft')
        SUBMITTED = 'submitted', _('Submitted')
        UNDER_REVIEW = 'under_review', _('Under Review')
        EVALUATED = 'evaluated', _('Evaluated')

    title = models.CharField(_('Title'), max_length=200)
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

    repo_url = models.URLField(_('Repository URL'), blank=True)
    demo_url = models.URLField(_('Demo URL'), blank=True)

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
