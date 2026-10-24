from django.db import models
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from accounts.models import User


class Hackathon(models.Model):
    """Represents a hackathon event."""
    name = models.CharField(max_length=200)
    track = models.CharField(max_length=100, blank=True)
    deadline = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['-created_at']


class Project(models.Model):
    """A project submitted to a hackathon."""
    hackathon = models.ForeignKey(Hackathon, on_delete=models.CASCADE, related_name='projects')
    name = models.CharField(max_length=200)
    team_name = models.CharField(max_length=150)
    github_url = models.URLField(blank=True)
    demo_url = models.URLField(blank=True)
    submitted_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.name} — {self.team_name}"

    class Meta:
        ordering = ['-submitted_at']


class EvaluationCriteria(models.Model):
    """A rubric criterion used to evaluate projects."""
    hackathon = models.ForeignKey(
        Hackathon, on_delete=models.CASCADE,
        related_name='criteria', null=True, blank=True,
        help_text=_("Leave blank to make this a global default criterion.")
    )
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    weight = models.PositiveSmallIntegerField(
        default=20,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
        help_text=_("Weight in % (all criteria for a hackathon should sum to 100).")
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        verbose_name_plural = "Evaluation Criteria"
        ordering = ['order', 'id']

    def __str__(self):
        scope = self.hackathon.name if self.hackathon else "Global"
        return f"[{scope}] {self.name} ({self.weight}%)"


class Evaluation(models.Model):
    """A jury member's full evaluation of a project."""

    class Status(models.TextChoices):
        PENDING = 'pending', _('Pending')
        IN_PROGRESS = 'in_progress', _('In Progress')
        COMPLETED = 'completed', _('Completed')

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='evaluations')
    evaluator = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='evaluations')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    # Qualitative feedback fields
    strengths = models.TextField(blank=True, verbose_name=_("Strengths & Highlights"))
    improvements = models.TextField(blank=True, verbose_name=_("Areas for Improvement"))
    confidential_notes = models.TextField(blank=True, verbose_name=_("Confidential Jury Summary"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('project', 'evaluator')
        ordering = ['-updated_at']

    def __str__(self):
        evaluator_name = self.evaluator.get_full_name() if self.evaluator else "Unknown"
        return f"{self.project.name} evaluated by {evaluator_name}"

    @property
    def total_score(self):
        """Compute the weighted aggregate score out of 100."""
        scores = self.scores.select_related('criteria').all()
        if not scores.exists():
            return 0
        total_weight = sum(s.criteria.weight for s in scores)
        if total_weight == 0:
            return 0
        weighted_sum = sum((s.score / 10) * s.criteria.weight for s in scores)
        return round(weighted_sum / total_weight * 100)


class EvaluationScore(models.Model):
    """A score for one specific criterion within an evaluation."""
    evaluation = models.ForeignKey(Evaluation, on_delete=models.CASCADE, related_name='scores')
    criteria = models.ForeignKey(EvaluationCriteria, on_delete=models.CASCADE, related_name='scores')
    score = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(10)]
    )

    class Meta:
        unique_together = ('evaluation', 'criteria')

    def __str__(self):
        return f"{self.criteria.name}: {self.score}/10"


class EvaluationComment(models.Model):
    """A comment thread on an evaluation (organizer ↔ jury review)."""
    evaluation = models.ForeignKey(Evaluation, on_delete=models.CASCADE, related_name='comments')
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='eval_comments')
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        author_name = self.author.get_full_name() if self.author else "Unknown"
        return f"Comment by {author_name} on {self.evaluation}"
