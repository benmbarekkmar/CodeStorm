from django.conf import settings
from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _


class Hackathon(models.Model):
    """Minimal hackathon model so teams can be associated with real events.

    This is intentionally small: it only carries the fields the Teams module
    needs (status, capacity defaults, organizer link). It can be expanded in
    place later without a second, competing model.
    """

    class Status(models.TextChoices):
        DRAFT = 'draft', _('Draft')
        REGISTRATION_OPEN = 'registration_open', _('Registration open')
        ONGOING = 'ongoing', _('Ongoing')
        CLOSED = 'closed', _('Closed')
        ARCHIVED = 'archived', _('Archived')

    name = models.CharField(_('Name'), max_length=200)
    slug = models.SlugField(_('Slug'), max_length=220, unique=True, blank=True)
    description = models.TextField(_('Description'), blank=True)
    status = models.CharField(
        _('Status'), max_length=20, choices=Status.choices, default=Status.DRAFT,
    )
    max_team_size = models.PositiveIntegerField(_('Default max team size'), default=5)
    allow_multiple_teams = models.BooleanField(
        _('Allow multiple teams per participant'),
        default=False,
        help_text=_('When off, a participant may only join one team per hackathon.'),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_('Organizer'),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='hackathons_created',
    )
    created_at = models.DateTimeField(_('Created at'), auto_now_add=True)
    updated_at = models.DateTimeField(_('Updated at'), auto_now=True)

    class Meta:
        verbose_name = _('Hackathon')
        verbose_name_plural = _('Hackathons')
        ordering = ('name',)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:220]
        super().save(*args, **kwargs)

    @property
    def accepts_participants(self):
        return self.status in (self.Status.REGISTRATION_OPEN, self.Status.ONGOING)
