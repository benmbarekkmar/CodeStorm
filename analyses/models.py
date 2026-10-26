from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class AIReport(models.Model):
    """A single AI decision-support report for a public GitHub repository.

    ``project`` is optional: a report can be launched on any public repository
    URL, and the reports recorded before the link existed stay valid. When it
    *is* set, the analysis was scored against that hackathon's real
    ``EvaluationCriterion`` rows, so each entry carries a ``criterion_id`` the
    jury can line up with its own rubric. There is still **no field that
    represents a final grade** -- scores live inside ``report['criteria']`` and
    are explicitly indicative.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', _('Pending')
        RUNNING = 'running', _('Running')
        DONE = 'done', _('Done')
        FAILED = 'failed', _('Failed')

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_('Created by'),
        on_delete=models.PROTECT,
        related_name='analysis_reports',
    )
    project = models.ForeignKey(
        'projects.Project',
        verbose_name=_('Project'),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ai_reports',
    )
    repository_url = models.URLField(_('Repository URL'), max_length=300)
    commit_ref = models.CharField(_('Commit reference'), max_length=64, blank=True)
    status = models.CharField(
        _('Status'), max_length=10, choices=Status.choices, default=Status.PENDING,
    )

    # Structured, validated AI output (summary/criteria/strengths/... ).
    report = models.JSONField(_('Report'), default=dict, blank=True)

    model_id = models.CharField(_('AI model'), max_length=120, blank=True)
    file_count = models.PositiveIntegerField(_('Files analyzed'), default=0)
    analyzed_bytes = models.PositiveIntegerField(_('Analyzed bytes'), default=0)

    # A safe, human-readable reason; never a traceback or secret.
    error_message = models.TextField(_('Error message'), blank=True)

    created_at = models.DateTimeField(_('Created at'), auto_now_add=True)
    updated_at = models.DateTimeField(_('Updated at'), auto_now=True)

    class Meta:
        verbose_name = _('AI report')
        verbose_name_plural = _('AI reports')
        ordering = ('-created_at', '-id')

    def __str__(self):
        return f'{self.repository_url} ({self.get_status_display()})'

    @property
    def author_display(self):
        return self.created_by.get_full_name() or self.created_by.username

    @property
    def is_done(self):
        return self.status == self.Status.DONE

    @property
    def is_project_bound(self):
        return self.project_id is not None

    @property
    def scale_max(self):
        """The scale the scores were produced on (5 for legacy reports)."""
        scale = self.report.get('scale') if isinstance(self.report, dict) else None
        if isinstance(scale, dict) and isinstance(scale.get('max'), int):
            return scale['max']
        return 5

    @property
    def criteria(self):
        return self.report.get('criteria', []) if isinstance(self.report, dict) else []

    @property
    def suggestion_by_criterion(self):
        """``criterion_id -> entry`` for the jury UI; ignores generic entries."""
        return {
            entry['criterion_id']: entry
            for entry in self.criteria
            if isinstance(entry, dict) and entry.get('criterion_id') is not None
        }

    @property
    def coverage(self):
        coverage = self.report.get('coverage') if isinstance(self.report, dict) else None
        return coverage if isinstance(coverage, dict) else {}

    @property
    def unverified_evidence(self):
        if not isinstance(self.report, dict):
            return 0
        value = self.report.get('unverified_evidence')
        return value if isinstance(value, int) else 0

    @property
    def summary(self):
        return self.report.get('summary', '') if isinstance(self.report, dict) else ''

    @property
    def technologies(self):
        return self.report.get('technologies', []) if isinstance(self.report, dict) else []

    @property
    def strengths(self):
        return self.report.get('strengths', []) if isinstance(self.report, dict) else []

    @property
    def concerns(self):
        return self.report.get('concerns', []) if isinstance(self.report, dict) else []

    @property
    def limitations(self):
        default = _('Results depend only on the files that could be analyzed.')
        if not isinstance(self.report, dict):
            return default
        return self.report.get('limitations') or default

    @property
    def indicative_average(self):
        """Mean of the indicative criteria scores (never a final grade)."""
        scores = [c.get('score') for c in self.criteria if isinstance(c.get('score'), int)]
        if not scores:
            return None
        return round(sum(scores) / len(scores), 1)

    @property
    def weighted_indicative(self):
        """Weighted indicative score, using the rubric weights captured at
        analysis time. The formula is the one ``Evaluation.get_weighted_score``
        already applies -- it is restated here over the stored report because a
        report must stay readable even if the rubric rows change afterwards.

        Returns ``None`` for reports scored on the generic rubric, which has no
        weights and therefore no comparable total.
        """
        entries = [c for c in self.criteria
                   if isinstance(c.get('score'), int) and isinstance(c.get('weight'), (int, float))]
        if not entries:
            return None
        total_weight = sum(float(c['weight']) for c in entries)
        if total_weight == 0:
            return None
        weighted = sum(float(c['score']) * float(c['weight']) for c in entries) / total_weight
        return round(weighted, 2)

    @classmethod
    def latest_done_for(cls, project):
        """Most recent completed report of this project, or ``None``."""
        return cls.objects.filter(
            project=project, status=cls.Status.DONE,
        ).first()
