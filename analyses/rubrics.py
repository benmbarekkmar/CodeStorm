"""Which rubric an analysis is scored against.

A project belongs to a hackathon that may already define ``EvaluationCriterion``
rows with weights; when it does, the AI must answer on those criteria and on the
same 0-10 scale ``EvaluationScore`` uses, otherwise the report would be a second
grading system next to the jury's. When nothing is defined yet, the generic
technical-review criteria are used and the report says so explicitly.
"""

from dataclasses import dataclass

from analyses.services import rubric
from evaluations.models import EvaluationCriterion

SOURCE_HACKATHON = 'hackathon'
SOURCE_GENERIC = 'generic'


@dataclass(frozen=True)
class RubricChoice:
    specs: tuple
    score_max: int
    source: str


def rubric_for_project(project):
    """Return the :class:`RubricChoice` to analyze ``project`` with."""
    if project is not None:
        rows = list(EvaluationCriterion.objects.filter(
            hackathon_id=project.hackathon_id,
        ).order_by('order', 'id'))
        if rows:
            return RubricChoice(
                specs=tuple(rubric.project_specs(rows)),
                score_max=rubric.PROJECT_SCORE_MAX,
                source=SOURCE_HACKATHON,
            )
    return RubricChoice(
        specs=tuple(rubric.generic_specs()),
        score_max=rubric.SCORE_MAX,
        source=SOURCE_GENERIC,
    )
