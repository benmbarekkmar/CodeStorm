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
    comment = models.TextField(_('General comment'), blank=True)
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

    def get_weighted_score(self):
        """
        Calcule la note pondérée de cette évaluation en tenant compte
        des poids de chaque critère défini pour le hackathon.
        Formule : Somme(score_critère × poids_critère) / Somme(poids_critères)
        Si aucun score n'existe, retourne None.
        """
        scores = self.scores.select_related('criterion').all()
        if not scores.exists():
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
