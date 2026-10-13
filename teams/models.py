from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _

from hackathons.models import Hackathon


class Team(models.Model):
    """Équipe participant à un hackathon."""

    name = models.CharField(_('Team name'), max_length=100)
    hackathon = models.ForeignKey(
        Hackathon,
        on_delete=models.CASCADE,
        related_name='teams',
        verbose_name=_('Hackathon'),
    )
    leader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='led_teams',
        verbose_name=_('Team leader'),
    )
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='teams',
        blank=True,
        verbose_name=_('Members'),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name = _('Team')
        verbose_name_plural = _('Teams')
        unique_together = [('name', 'hackathon')]

    def __str__(self):
        return f"{self.name} ({self.hackathon.name})"
