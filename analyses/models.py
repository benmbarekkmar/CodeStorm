from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class AIReport(models.Model):
    """A single AI decision-support report for a public GitHub repository.

    Deliberately self-contained: it references only the existing custom User
    model and stores the repository location as plain text. There is no
    foreign key to ``Project``/``Evaluation`` (those apps are still evolving)
    and **no field represents a final grade** -- scores live inside
    ``report['criteria']`` and are explicitly indicative.
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
    def criteria(self):
        return self.report.get('criteria', []) if isinstance(self.report, dict) else []

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
