"""Bridging a stored report to the jury's scoring form.

The jury evaluates a project through ``evaluations``; this module only reshapes
an already validated report into JSON-friendly suggestions. It never writes a
score: an indicative number leaves here as a *proposal* the juré can adopt,
refuse or ignore, and the authoritative value stays whatever
``EvaluationScore`` holds.
"""

from django.urls import reverse

from analyses.models import AIReport
from analyses.permissions import can_view


def _entry_payload(entry, scale_max):
    return {
        'criterion_id': entry.get('criterion_id'),
        'name': entry.get('label', ''),
        'key': entry.get('key'),
        'indicative_score': entry.get('score'),
        'scale_max': scale_max,
        'weight': entry.get('weight'),
        'rationale': entry.get('rationale', ''),
        'evidence': list(entry.get('evidence') or []),
    }


def _payload(report, *, with_report):
    data = {
        'report_id': report.pk,
        'report_url': reverse('analyses:detail', args=[report.pk]),
        'repository_url': report.repository_url,
        'status': report.status,
        'created_at': report.created_at.isoformat(),
        'model_id': report.model_id,
        'file_count': report.file_count,
    }
    if not with_report:
        # An analysis that did not finish carries no findings to suggest.
        data['error_message'] = report.error_message
        return data
    data.update({
        'scale_max': report.scale_max,
        'summary': report.summary,
        'technologies': list(report.technologies),
        'strengths': list(report.strengths),
        'concerns': list(report.concerns),
        'limitations': report.limitations,
        'unverified_evidence': report.unverified_evidence,
        'indicative_average': report.indicative_average,
        'weighted_indicative': report.weighted_indicative,
        'criteria': [
            _entry_payload(entry, report.scale_max)
            for entry in report.criteria if isinstance(entry, dict)
        ],
    })
    return data


def analysis_state_for(user, project):
    """Latest analysis of this project, as the jury may see it, or ``None``.

    ``None`` means "no visible analysis", which the UI renders as "none yet"; a
    payload whose ``status`` is not ``done`` carries the state only.
    """
    if project is None:
        return None
    report = AIReport.objects.filter(project=project).first()
    if report is None or not can_view(user, report):
        return None
    return _payload(report, with_report=report.is_done)


def suggestions_for_user(user, project):
    """``criterion_id -> suggestion`` for a visible, completed report."""
    state = analysis_state_for(user, project)
    if not state or state['status'] != AIReport.Status.DONE:
        return {}
    return {
        entry['criterion_id']: entry
        for entry in state.get('criteria', [])
        if entry.get('criterion_id') is not None
    }
