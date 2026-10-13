import uuid
from django.db import models
from django.db.models import Q
from django.core.exceptions import ValidationError
from django.conf import settings
from django.core.validators import MinValueValidator
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from hackathons.models import Hackathon
from projects.models import Project


# ---------------------------------------------------------------------------
# Sponsor
# ---------------------------------------------------------------------------

class Sponsor(models.Model):
    """
    Sponsor associé à un ou plusieurs prix.
    Contient les informations publiques du sponsor (nom, description, site).
    """
    name = models.CharField(_('Sponsor name'), max_length=150)
    description = models.TextField(_('Description'), blank=True)
    website = models.URLField(_('Website'), blank=True)
    logo = models.ImageField(
        _('Logo'), upload_to='sponsors/logos/', blank=True, null=True
    )
    contact_email = models.EmailField(_('Contact email'), blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_sponsors',
        verbose_name=_('Created by'),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name = _('Sponsor')
        verbose_name_plural = _('Sponsors')

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Prize Category
# ---------------------------------------------------------------------------

class PrizeCategory(models.Model):
    """
    Catégorie de prix configurable (ex. Grand Prix, Meilleure Innovation...).
    Centralisées ici pour éviter les chaînes codées en dur partout.
    """
    name = models.CharField(_('Category name'), max_length=100, unique=True)
    description = models.TextField(_('Description'), blank=True)
    icon = models.CharField(
        _('Material icon name'), max_length=60, blank=True,
        help_text=_('Google Material Symbols icon name, e.g. "emoji_events"')
    )
    order = models.PositiveIntegerField(_('Display order'), default=0)

    class Meta:
        ordering = ['order', 'name']
        constraints = [
            models.UniqueConstraint(Lower('name'), name='unique_prize_category_name_ci'),
        ]
        verbose_name = _('Prize Category')
        verbose_name_plural = _('Prize Categories')

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Prize
# ---------------------------------------------------------------------------

class Prize(models.Model):
    """
    Prix défini dans le cadre d'un hackathon.
    Un hackathon peut avoir plusieurs prix.
    Chaque prix peut être rattaché à une catégorie, un sponsor, et
    un projet gagnant (après validation du classement).
    """

    class Status(models.TextChoices):
        AVAILABLE = 'available', _('Available')
        ASSIGNED = 'assigned', _('Assigned — awaiting confirmation')
        AWARDED = 'awarded', _('Awarded — officially confirmed')

    name = models.CharField(_('Prize name'), max_length=200)
    description = models.TextField(_('Description'), blank=True)

    hackathon = models.ForeignKey(
        Hackathon,
        on_delete=models.CASCADE,
        related_name='prizes',
        verbose_name=_('Hackathon'),
    )
    category = models.ForeignKey(
        PrizeCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='prizes',
        verbose_name=_('Category'),
    )
    sponsor = models.ForeignKey(
        Sponsor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='prizes',
        verbose_name=_('Sponsor'),
    )

    # Récompense
    reward = models.CharField(
        _('Reward description'),
        max_length=300,
        blank=True,
        help_text=_('Brief text describing the reward (e.g. Cloud credits, internship...)'),
    )
    amount = models.DecimalField(
        _('Cash amount (USD)'),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )

    # Gagnant — assigné après validation du classement
    winning_project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='prizes_won',
        verbose_name=_('Winning project'),
    )

    # Contrôle des slots (ex. 1 gagnant ou plusieurs ex-aequo)
    winner_slots = models.PositiveIntegerField(
        _('Number of winner slots'),
        default=1,
        validators=[MinValueValidator(1)],
    )

    status = models.CharField(
        _('Status'),
        max_length=20,
        choices=Status.choices,
        default=Status.AVAILABLE,
    )

    eligibility_criteria = models.TextField(
        _('Eligibility criteria'), blank=True,
        help_text=_('Describes who can receive this prize.')
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['hackathon', '-amount', 'name']
        verbose_name = _('Prize')
        verbose_name_plural = _('Prizes')
        # Empêche deux prix du même nom dans le même hackathon
        unique_together = [('hackathon', 'name')]
        constraints = [
            models.UniqueConstraint(
                Lower('name'), 'hackathon',
                name='unique_prize_name_per_hackathon_ci',
            ),
        ]

    def __str__(self):
        return f"{self.name} — {self.hackathon.name}"

    def clean(self):
        super().clean()
        if self.winning_project_id and self.hackathon_id:
            if self.winning_project.hackathon_id != self.hackathon_id:
                raise ValidationError({
                    'winning_project': _('The winning project must belong to this hackathon.')
                })

    @property
    def is_awarded(self):
        return self.status == self.Status.AWARDED

    @property
    def has_winner(self):
        return self.winning_project is not None


# ---------------------------------------------------------------------------
# Leaderboard Entry
# ---------------------------------------------------------------------------

class LeaderboardEntry(models.Model):
    """
    Entrée du classement final pour un hackathon.
    Calculée par l'organisateur à partir des évaluations.
    La publication du classement est contrôlée par hackathon.results_published.
    """

    hackathon = models.ForeignKey(
        Hackathon,
        on_delete=models.CASCADE,
        related_name='leaderboard_entries',
        verbose_name=_('Hackathon'),
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='leaderboard_entries',
        verbose_name=_('Project'),
    )

    rank = models.PositiveIntegerField(
        _('Rank'),
        help_text=_('1 = First place.'),
    )

    # Score final agrégé (calculé depuis les évaluations)
    final_score = models.DecimalField(
        _('Final score'),
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
    )

    # Note par critère agrégée (sauvegardée pour historique)
    score_breakdown = models.JSONField(
        _('Score breakdown'),
        default=dict,
        blank=True,
        help_text=_('JSON dict: {criterion_name: averaged_score}'),
    )

    notes = models.TextField(
        _('Organizer notes'), blank=True,
        help_text=_('Private notes visible only to organizers.')
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['hackathon', 'rank']
        verbose_name = _('Leaderboard Entry')
        verbose_name_plural = _('Leaderboard Entries')
        # Plusieurs projets peuvent partager un rang en cas d'égalité.
        constraints = [
            models.UniqueConstraint(
                fields=('hackathon', 'project'),
                name='unique_leaderboard_project_per_hackathon',
            ),
        ]

    def __str__(self):
        return f"#{self.rank} — {self.project.title} ({self.hackathon.name})"


# ---------------------------------------------------------------------------
# Certificate
# ---------------------------------------------------------------------------

class Certificate(models.Model):
    """
    Certificat officiel délivré à un participant ou une équipe.
    Généré après publication des résultats.
    Identifiant unique pour vérification d'authenticité.
    """

    class CertType(models.TextChoices):
        WINNER = 'winner', _('Winner Certificate')
        FINALIST = 'finalist', _('Finalist Certificate')
        PARTICIPATION = 'participation', _('Participation Certificate')
        SPECIAL_AWARD = 'special_award', _('Special Award Certificate')

    # Identifiant unique imprimé sur le certificat
    uid = models.UUIDField(
        _('Certificate UID'),
        default=uuid.uuid4,
        unique=True,
        editable=False,
    )

    cert_type = models.CharField(
        _('Certificate type'),
        max_length=20,
        choices=CertType.choices,
        default=CertType.PARTICIPATION,
    )

    # Bénéficiaire individuel (participant)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='certificates',
        verbose_name=_('Recipient'),
    )

    hackathon = models.ForeignKey(
        Hackathon,
        on_delete=models.PROTECT,
        related_name='certificates',
        verbose_name=_('Hackathon'),
    )

    # Prix associé (pour les certificats de type winner/special)
    prize = models.ForeignKey(
        Prize,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='certificates',
        verbose_name=_('Associated prize'),
    )

    # Position dans le classement (si applicable)
    rank = models.PositiveIntegerField(
        _('Rank'), null=True, blank=True
    )

    issued_at = models.DateTimeField(
        _('Issue date'), auto_now_add=True
    )

    # Chemin vers le fichier PDF généré
    pdf_file = models.FileField(
        _('PDF file'),
        upload_to='certificates/pdfs/',
        blank=True,
        null=True,
    )

    class Meta:
        ordering = ['-issued_at']
        verbose_name = _('Certificate')
        verbose_name_plural = _('Certificates')
        # Empêche les doublons : un certificat par type, par hackathon, par bénéficiaire
        constraints = [
            models.UniqueConstraint(
                fields=('recipient', 'hackathon', 'cert_type', 'prize'),
                condition=Q(prize__isnull=False),
                name='unique_prize_certificate_per_recipient',
            ),
            models.UniqueConstraint(
                fields=('recipient', 'hackathon', 'cert_type'),
                condition=Q(prize__isnull=True),
                name='unique_general_certificate_per_recipient',
            ),
        ]

    def __str__(self):
        return (
            f"{self.get_cert_type_display()} — "
            f"{self.recipient.get_full_name() or self.recipient.username} "
            f"({self.hackathon.name})"
        )

    @property
    def short_uid(self):
        """Affiche les 8 premiers caractères du UUID pour les certificats."""
        return str(self.uid)[:8].upper()


class PrizeAward(models.Model):
    """Affectation d'un prix à un projet classé, une ligne par gagnant."""

    prize = models.ForeignKey(
        Prize,
        on_delete=models.CASCADE,
        related_name='awards',
        verbose_name=_('Prize'),
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='prize_awards',
        verbose_name=_('Winning project'),
    )
    position = models.PositiveIntegerField(
        _('Winner slot'),
        validators=[MinValueValidator(1)],
        help_text=_('Slot number within the prize, from 1 to the configured winner slots.'),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('prize_id', 'position')
        constraints = [
            models.UniqueConstraint(fields=('prize', 'project'), name='unique_project_per_prize'),
            models.UniqueConstraint(fields=('prize', 'position'), name='unique_winner_slot_per_prize'),
        ]
        verbose_name = _('Prize award')
        verbose_name_plural = _('Prize awards')

    def clean(self):
        super().clean()
        errors = {}
        if self.prize_id and self.project_id:
            if self.prize.hackathon_id != self.project.hackathon_id:
                errors['project'] = _('The winning project must belong to the prize hackathon.')
        if self.prize_id and self.position and self.position > self.prize.winner_slots:
            errors['position'] = _('This prize has fewer winner slots than the selected position.')
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f'{self.prize.name} — {self.project.title} (slot {self.position})'
