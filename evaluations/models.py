from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils.translation import gettext_lazy as _

from hackathons.models import Hackathon
from projects.models import Project


class EvaluationCriterion(models.Model):
    """
    Critère d'évaluation défini pour un hackathon.
    Exemple : Innovation (poids 40%), Technique (poids 40%), Impact (poids 20%).
    """
    hackathon = models.ForeignKey(
        Hackathon,
        on_delete=models.CASCADE,
        related_name='criteria',
        verbose_name=_('Hackathon'),
    )
    name = models.CharField(_('Criterion name'), max_length=100)
    description = models.TextField(_('Description'), blank=True)
    weight = models.DecimalField(
        _('Weight (%)'),
        max_digits=5,
        decimal_places=2,
        default=100,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text=_('Percentage weight of this criterion in the final score.'),
    )
    order = models.PositiveIntegerField(_('Display order'), default=0)

    class Meta:
        ordering = ['order', 'name']
        verbose_name = _('Evaluation Criterion')
        verbose_name_plural = _('Evaluation Criteria')

    def __str__(self):
        return f"{self.hackathon.title} — {self.name} ({self.weight}%)"


class Evaluation(models.Model):
    """
    Évaluation d'un projet par un membre du jury.
    Chaque juré ne peut évaluer un projet qu'une seule fois.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', _('Pending')
        IN_PROGRESS = 'in_progress', _('In Progress')
        COMPLETED = 'completed', _('Completed')

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='evaluations',
        verbose_name=_('Project'),
    )
    jury = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='evaluations',
        limit_choices_to={'role': 'jury'},
        verbose_name=_('Jury member'),
    )
    status = models.CharField(
        _('Status'),
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    comment = models.TextField(_('General comment'), blank=True)
    strengths = models.TextField(_('Strengths & Highlights'), blank=True)
    improvements = models.TextField(_('Areas for Improvement'), blank=True)
    confidential_notes = models.TextField(_('Confidential Jury Summary'), blank=True)
    is_complete = models.BooleanField(
        _('Evaluation complete'),
        default=False,
        help_text=_('Mark as complete when all scores have been assigned.'),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('project', 'jury')]
        ordering = ['-created_at']
        verbose_name = _('Evaluation')
        verbose_name_plural = _('Evaluations')

    def __str__(self):
        return f"{self.jury.username} → {self.project.title}"

    def save(self, *args, **kwargs):
        # ``is_complete`` stays the flag prizes/services.py validates, so the
        # lifecycle status can never claim "completed" ahead of it; the reverse
        # direction keeps the dashboard honest when a juré submits.
        if self.is_complete and self.status != self.Status.COMPLETED:
            self.status = self.Status.COMPLETED
        super().save(*args, **kwargs)

    @property
    def total_score(self):
        """The weighted score on the 0..100 scale the jury dashboard displays.

        This is :meth:`get_weighted_score` rescaled, not a second formula: one
        evaluation can only ever have one weighted result for the dashboard, the
        leaderboard and the prizes ranking.
        """
        weighted = self.get_weighted_score()
        if weighted is None:
            return 0
        return int(round(float(weighted) * 10))

    def get_weighted_score(self):
        """
        Calcule la note pondérée de cette évaluation en tenant compte
        des poids de chaque critère défini pour le hackathon.
        Formule : Somme(score_critère × poids_critère) / Somme(poids_critères)
        Si aucun score n'existe, retourne None.

        Les appels en liste (tableau de bord du jury, classement, préparation des
        prix) préchargent ``scores__criterion`` : on lit ce cache au lieu de
        renvoyer une requête par évaluation.
        """
        scores = list(self.scores.all())
        if not scores:
            return None

        total_weighted = sum(
            float(s.score) * float(s.criterion.weight)
            for s in scores
        )
        total_weight = sum(float(s.criterion.weight) for s in scores)
        if total_weight == 0:
            return None
        return round(total_weighted / total_weight, 2)


class EvaluationScore(models.Model):
    """
    Note donnée par un juré pour un critère spécifique d'une évaluation.
    Score entre 0 et 10.
    """
    evaluation = models.ForeignKey(
        Evaluation,
        on_delete=models.CASCADE,
        related_name='scores',
        verbose_name=_('Evaluation'),
    )
    criterion = models.ForeignKey(
        EvaluationCriterion,
        on_delete=models.CASCADE,
        related_name='scores',
        verbose_name=_('Criterion'),
    )
    score = models.DecimalField(
        _('Score'),
        max_digits=4,
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(10)],
    )

    class Meta:
        unique_together = [('evaluation', 'criterion')]
        verbose_name = _('Evaluation Score')
        verbose_name_plural = _('Evaluation Scores')

    def __str__(self):
        return f"{self.evaluation} — {self.criterion.name}: {self.score}/10"


class EvaluationComment(models.Model):
    """
    Fil de commentaires sur une évaluation (échange organisateur ↔ jury).

    Stocké séparément des ``confidential_notes`` : un commentaire est partagé,
    alors que le résumé du jury reste confidentiel.
    """
    evaluation = models.ForeignKey(
        Evaluation,
        on_delete=models.CASCADE,
        related_name='comments',
        verbose_name=_('Evaluation'),
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='evaluation_comments',
        verbose_name=_('Author'),
    )
    text = models.TextField(_('Comment'))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = _('Evaluation Comment')
        verbose_name_plural = _('Evaluation Comments')

    def __str__(self):
        author = self.author.get_full_name() if self.author else _('Unknown')
        return f"{author} → {self.evaluation.project.title}"
